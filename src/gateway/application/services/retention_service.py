from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.gateway.application.ports.object_storage import IVersionedObjectStorage


logger = logging.getLogger(__name__)
MIN_ARCHIVE_RETENTION = timedelta(days=365)
MIN_REVISION_RETENTION = timedelta(days=1095)
MIN_OPERATIONAL_RETENTION = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class RetentionResult:
    purged_counts: dict[str, int]
    deleted_storage_keys: tuple[str, ...]
    failed_storage_keys: tuple[str, ...]


class RetentionService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        storage: IVersionedObjectStorage,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage

    async def purge(
        self,
        *,
        now: datetime,
        archive_retention: timedelta,
        revision_retention: timedelta,
        operational_retention: timedelta,
        batch_size: int,
    ) -> RetentionResult | None:
        if batch_size < 1 or batch_size > 1000:
            raise ValueError("Retention batch size must be between 1 and 1000")
        if any(
            duration.total_seconds() <= 0
            for duration in (archive_retention, revision_retention, operational_retention)
        ):
            raise ValueError("Retention durations must be positive")
        if archive_retention < MIN_ARCHIVE_RETENTION:
            raise ValueError("Archive retention must be at least 365 days")
        if revision_retention < MIN_REVISION_RETENTION:
            raise ValueError("Revision retention must be at least 1095 days")
        if operational_retention < MIN_OPERATIONAL_RETENTION:
            raise ValueError("Operational retention must be at least 30 days")
        async with self._session_factory.begin() as session:
            if session.bind.dialect.name != "postgresql":
                raise RuntimeError("Retention requires PostgreSQL")
            acquired = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(739814621451062318)")
            )
            if not acquired:
                return None
            db_now = await session.scalar(text("SELECT statement_timestamp()"))
            rows = (
                await session.execute(
                    text(
                        "SELECT record_type, record_id, storage_key "
                        "FROM public.gateway_run_retention(:archived_before, :revision_before, :operational_before, :max_rows)"
                    ),
                    {
                        "archived_before": db_now - archive_retention,
                        "revision_before": db_now - revision_retention,
                        "operational_before": db_now - operational_retention,
                        "max_rows": batch_size,
                    },
                )
            ).mappings().all()
        identifiers = {
            (str(row["record_type"]), str(row["record_id"]))
            for row in rows
        }
        counts = Counter(record_type for record_type, _ in identifiers)
        storage_keys = tuple(
            sorted({str(row["storage_key"]) for row in rows if row["storage_key"]})
        )
        deleted = []
        failed = []
        for storage_key in storage_keys:
            try:
                if await self._storage.delete(storage_key):
                    deleted.append(storage_key)
            except Exception:
                failed.append(storage_key)
                logger.exception(
                    "Retention left an unreferenced storage object for reconciliation",
                    extra={"storage_key": storage_key},
                )
        return RetentionResult(
            purged_counts=dict(sorted(counts.items())),
            deleted_storage_keys=tuple(deleted),
            failed_storage_keys=tuple(failed),
        )


class RetentionMaintenanceRunner:
    def __init__(
        self,
        service: RetentionService,
        *,
        interval_seconds: float,
        archive_days: int,
        revision_days: int,
        operational_days: int,
        batch_size: int,
        max_batches_per_cycle: int,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Retention interval must be positive")
        if max_batches_per_cycle < 1 or max_batches_per_cycle > 1000:
            raise ValueError("Retention cycle must contain between 1 and 1000 batches")
        self._service = service
        self._interval_seconds = interval_seconds
        self._archive_retention = timedelta(days=archive_days)
        self._revision_retention = timedelta(days=revision_days)
        self._operational_retention = timedelta(days=operational_days)
        self._batch_size = batch_size
        self._max_batches_per_cycle = max_batches_per_cycle

    async def run_once(self) -> RetentionResult | None:
        return await self._service.purge(
            now=datetime.now(timezone.utc),
            archive_retention=self._archive_retention,
            revision_retention=self._revision_retention,
            operational_retention=self._operational_retention,
            batch_size=self._batch_size,
        )

    async def run_cycle(self, stop_event: asyncio.Event | None = None) -> RetentionResult | None:
        counts: Counter[str] = Counter()
        deleted: set[str] = set()
        failed: set[str] = set()
        completed_batch = False
        for _ in range(self._max_batches_per_cycle):
            if stop_event is not None and stop_event.is_set():
                break
            result = await self.run_once()
            if result is None:
                break
            completed_batch = True
            counts.update(result.purged_counts)
            deleted.update(result.deleted_storage_keys)
            failed.update(result.failed_storage_keys)
            if not result.purged_counts and not result.deleted_storage_keys and not result.failed_storage_keys:
                break
            await asyncio.sleep(0)
        if not completed_batch:
            return None
        return RetentionResult(
            purged_counts=dict(sorted(counts.items())),
            deleted_storage_keys=tuple(sorted(deleted)),
            failed_storage_keys=tuple(sorted(failed)),
        )

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_cycle(stop_event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Retention maintenance iteration failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                pass
