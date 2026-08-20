from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
import random
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.gateway.domain.exceptions import JobLeaseLostException
from src.gateway.infrastructure.persistence.ingestion_models import JobOutboxModel


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    event_id: UUID
    event_type: str
    payload: dict
    deduplication_key: str
    claim_token: UUID


class JobOutboxService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_next(self, worker_id: str, *, lease_seconds: int) -> OutboxClaim | None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("An outbox worker id and positive lease duration are required")
        now = await self._session.scalar(select(func.now()))
        await self._session.execute(
            update(JobOutboxModel)
            .where(
                or_(
                    and_(
                        JobOutboxModel.state == "pending",
                        JobOutboxModel.available_at <= now,
                    ),
                    and_(
                        JobOutboxModel.state == "publishing",
                        JobOutboxModel.lease_expires_at <= now,
                    ),
                ),
                JobOutboxModel.attempt_count >= JobOutboxModel.max_attempts,
            )
            .values(
                state="failed",
                last_error_code="attempts_exhausted",
                lease_owner=None,
                lease_expires_at=None,
                claim_token=None,
                updated_at=now,
            )
        )
        event = await self._session.scalar(
            select(JobOutboxModel)
            .where(
                or_(
                    and_(
                        JobOutboxModel.state == "pending",
                        JobOutboxModel.available_at <= now,
                    ),
                    and_(
                        JobOutboxModel.state == "publishing",
                        JobOutboxModel.lease_expires_at <= now,
                    ),
                ),
                JobOutboxModel.attempt_count < JobOutboxModel.max_attempts,
            )
            .order_by(JobOutboxModel.available_at, JobOutboxModel.created_at, JobOutboxModel.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if event is None:
            return None
        token = uuid4()
        event.state = "publishing"
        event.attempt_count += 1
        event.lease_owner = worker_id.strip()
        event.lease_expires_at = now + timedelta(seconds=lease_seconds)
        event.claim_token = token
        event.updated_at = now
        await self._session.flush()
        return OutboxClaim(
            event_id=event.id,
            event_type=event.event_type,
            payload=dict(event.payload),
            deduplication_key=event.deduplication_key,
            claim_token=token,
        )

    async def complete(self, claim: OutboxClaim) -> None:
        now = await self._session.scalar(select(func.now()))
        result = await self._session.execute(
            update(JobOutboxModel)
            .where(
                JobOutboxModel.id == claim.event_id,
                JobOutboxModel.state == "publishing",
                JobOutboxModel.claim_token == claim.claim_token,
                JobOutboxModel.lease_expires_at > now,
            )
            .values(
                state="published",
                published_at=now,
                last_error_code=None,
                lease_owner=None,
                lease_expires_at=None,
                claim_token=None,
                updated_at=now,
            )
            .returning(JobOutboxModel.id)
        )
        if result.scalar_one_or_none() is None:
            raise JobLeaseLostException()

    async def fail(self, claim: OutboxClaim, *, error_code: str) -> str:
        now = await self._session.scalar(select(func.now()))
        event = await self._session.scalar(
            select(JobOutboxModel)
            .where(
                JobOutboxModel.id == claim.event_id,
                JobOutboxModel.state == "publishing",
                JobOutboxModel.claim_token == claim.claim_token,
                JobOutboxModel.lease_expires_at > now,
            )
            .with_for_update()
        )
        if event is None:
            raise JobLeaseLostException()
        event.last_error_code = error_code[:64]
        event.lease_owner = None
        event.lease_expires_at = None
        event.claim_token = None
        event.updated_at = now
        if event.attempt_count >= event.max_attempts:
            event.state = "failed"
        else:
            delay = min(5 * (2 ** max(event.attempt_count - 1, 0)), 300)
            jitter = random.SystemRandom().uniform(0, min(delay * 0.2, 30))
            event.state = "pending"
            event.available_at = now + timedelta(seconds=delay + jitter)
        await self._session.flush()
        return event.state


class JobOutboxDispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        publisher: Callable[[str, dict, str], Awaitable[None]],
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("An outbox worker id and positive lease duration are required")
        self._session_factory = session_factory
        self._publisher = publisher
        self._worker_id = worker_id.strip()
        self._lease_seconds = lease_seconds

    async def dispatch_once(self) -> UUID | None:
        async with self._session_factory.begin() as session:
            claim = await JobOutboxService(session).claim_next(
                self._worker_id,
                lease_seconds=self._lease_seconds,
            )
        if claim is None:
            return None
        try:
            await self._publisher(
                claim.event_type,
                claim.payload,
                claim.deduplication_key,
            )
        except Exception as exc:
            async with self._session_factory.begin() as session:
                await JobOutboxService(session).fail(
                    claim,
                    error_code=f"publish_{type(exc).__name__.lower()}",
                )
            return claim.event_id
        async with self._session_factory.begin() as session:
            await JobOutboxService(session).complete(claim)
        return claim.event_id

    async def run_until_stopped(
        self,
        stop_event: asyncio.Event,
        *,
        idle_delay_seconds: float = 0.5,
    ) -> None:
        if idle_delay_seconds <= 0:
            raise ValueError("Outbox idle delay must be positive")
        while not stop_event.is_set():
            dispatched = await self.dispatch_once()
            if dispatched is None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=idle_delay_seconds)
                except TimeoutError:
                    pass
