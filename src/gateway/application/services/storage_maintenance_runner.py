from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.gateway.application.services.storage_consistency_service import StorageConsistencyResult, StorageConsistencyService


class StorageMaintenanceRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        consistency: StorageConsistencyService,
        *,
        interval_seconds: float,
        staging_ttl_seconds: float,
        orphan_grace_seconds: float,
    ) -> None:
        if interval_seconds <= 0 or staging_ttl_seconds <= 0 or orphan_grace_seconds <= 0:
            raise ValueError("Storage maintenance durations must be positive")
        self._session_factory = session_factory
        self._consistency = consistency
        self._interval_seconds = interval_seconds
        self._staging_ttl = timedelta(seconds=staging_ttl_seconds)
        self._orphan_grace = timedelta(seconds=orphan_grace_seconds)

    async def run_once(self) -> StorageConsistencyResult | None:
        async with self._session_factory() as probe:
            dialect = probe.bind.dialect.name
        if dialect != "postgresql":
            return await self._consistency.reconcile(
                now=datetime.now(timezone.utc),
                staging_ttl=self._staging_ttl,
                orphan_grace=self._orphan_grace,
            )
        async with self._session_factory.begin() as session:
            acquired = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(739814621451062317)")
            )
            if not acquired:
                return None
            return await self._consistency.reconcile(
                now=datetime.now(timezone.utc),
                staging_ttl=self._staging_ttl,
                orphan_grace=self._orphan_grace,
            )

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                pass
