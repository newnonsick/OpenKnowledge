from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.domain.exceptions import ResourceConflictException
from src.gateway.infrastructure.persistence.identity_models import IdempotencyRecordModel


class ReservationStatus(StrEnum):
    RESERVED = "reserved"
    IN_PROGRESS = "in_progress"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class IdempotencyReservation:
    record_id: UUID
    status: ReservationStatus
    response_status: int | None = None
    resource_ids: tuple[str, ...] = ()


class IdempotencyService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        retention: timedelta = timedelta(hours=24),
    ) -> None:
        self._session = session
        self._retention = retention

    async def reserve(
        self,
        *,
        actor_id: str,
        operation: str,
        idempotency_key: str,
        payload,
        now: datetime | None = None,
    ) -> IdempotencyReservation:
        if not actor_id or len(actor_id) > 128:
            raise ValueError("Invalid idempotency actor")
        if not operation or len(operation) > 128:
            raise ValueError("Invalid idempotency operation")
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("Invalid idempotency key")
        current_time = now or datetime.now(timezone.utc)
        request_hash = self.request_hash(payload)
        candidate = IdempotencyRecordModel(
            id=uuid4(),
            actor_id=actor_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            created_at=current_time,
            expires_at=current_time + self._retention,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(candidate)
                await self._session.flush()
            return IdempotencyReservation(candidate.id, ReservationStatus.RESERVED)
        except IntegrityError:
            existing = await self._session.scalar(
                select(IdempotencyRecordModel)
                .where(
                    IdempotencyRecordModel.actor_id == actor_id,
                    IdempotencyRecordModel.operation == operation,
                    IdempotencyRecordModel.idempotency_key == idempotency_key,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if existing is None:
                raise ResourceConflictException("Idempotency reservation conflict.")
            if existing.expires_at <= current_time:
                existing.request_hash = request_hash
                existing.response_status = None
                existing.resource_ids = []
                existing.created_at = current_time
                existing.expires_at = current_time + self._retention
                await self._session.flush()
                return IdempotencyReservation(existing.id, ReservationStatus.RESERVED)
            if existing.request_hash != request_hash:
                raise ResourceConflictException(
                    "Idempotency key was already used for a different request."
                )
            if existing.response_status is None:
                return IdempotencyReservation(existing.id, ReservationStatus.IN_PROGRESS)
            return IdempotencyReservation(
                existing.id,
                ReservationStatus.REPLAY,
                existing.response_status,
                tuple(str(value) for value in existing.resource_ids),
            )

    async def complete(
        self,
        record_id: UUID,
        *,
        response_status: int,
        resource_ids: list[str] | tuple[str, ...],
    ) -> None:
        if response_status < 100 or response_status > 599:
            raise ValueError("Invalid response status")
        record = await self._session.scalar(
            select(IdempotencyRecordModel)
            .where(IdempotencyRecordModel.id == record_id)
            .with_for_update()
        )
        if record is None:
            raise ResourceConflictException("Idempotency reservation is unavailable.")
        normalized_resource_ids = [str(value) for value in resource_ids]
        if record.response_status is not None:
            if (
                record.response_status == response_status
                and record.resource_ids == normalized_resource_ids
            ):
                return
            raise ResourceConflictException(
                "Idempotency result has already been completed."
            )
        record.response_status = response_status
        record.resource_ids = normalized_resource_ids
        await self._session.flush()

    @staticmethod
    def request_hash(payload) -> str:
        try:
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Idempotency payload must be canonical JSON") from exc
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
