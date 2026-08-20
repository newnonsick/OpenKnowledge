from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import random
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.domain.exceptions import ConcurrencyConflictException, ItemNotFoundException, JobLeaseLostException
from src.gateway.infrastructure.persistence.ingestion_models import DocumentRevisionModel, IngestionJobModel


@dataclass(frozen=True, slots=True)
class JobClaim:
    job_id: UUID
    space_id: str
    document_id: UUID
    document_revision_id: UUID
    claim_token: UUID
    attempt_count: int
    cancellation_requested: bool
    queued_at: datetime


class IngestionJobService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_next(self, worker_id: str, *, lease_seconds: int) -> JobClaim | None:
        if not worker_id.strip() or lease_seconds <= 0:
            raise ValueError("A worker id and positive lease duration are required")
        now = await self._database_now()
        await self._activate_retry_requests(now)
        await self._finalize_cancellation_requests(now)
        await self._finalize_exhausted(now)
        claimable = or_(
            and_(
                IngestionJobModel.state.in_(("queued", "retry_wait")),
                IngestionJobModel.next_attempt_at <= now,
                IngestionJobModel.cancellation_requested.is_(False),
            ),
            and_(
                IngestionJobModel.state == "running",
                IngestionJobModel.lease_expires_at <= now,
            ),
        )
        job = await self._session.scalar(
            select(IngestionJobModel)
            .where(claimable, IngestionJobModel.attempt_count < IngestionJobModel.max_attempts)
            .order_by(IngestionJobModel.next_attempt_at, IngestionJobModel.created_at, IngestionJobModel.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        token = uuid4()
        job.state = "running"
        job.lease_owner = worker_id.strip()
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.claim_token = token
        job.attempt_count += 1
        job.started_at = job.started_at or now
        job.updated_at = now
        await self._session.flush()
        return JobClaim(
            job_id=job.id,
            space_id=job.space_id,
            document_id=job.document_id,
            document_revision_id=job.document_revision_id,
            claim_token=token,
            attempt_count=job.attempt_count,
            cancellation_requested=job.cancellation_requested,
            queued_at=job.created_at,
        )

    async def queue_depths(self) -> dict[str, int]:
        rows = await self._session.execute(
            select(IngestionJobModel.state, func.count(IngestionJobModel.id))
            .where(IngestionJobModel.state.in_(("queued", "retry_wait", "running")))
            .group_by(IngestionJobModel.state)
        )
        counts = {state: count for state, count in rows}
        return {state: int(counts.get(state, 0)) for state in ("queued", "retry_wait", "running")}

    async def heartbeat(self, job_id: UUID, claim_token: UUID, *, lease_seconds: int) -> None:
        if lease_seconds <= 0:
            raise ValueError("A positive lease duration is required")
        now = await self._database_now()
        result = await self._session.execute(
            update(IngestionJobModel)
            .where(
                IngestionJobModel.id == job_id,
                IngestionJobModel.state == "running",
                IngestionJobModel.claim_token == claim_token,
                IngestionJobModel.cancellation_requested.is_(False),
                IngestionJobModel.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
            .returning(IngestionJobModel.id)
        )
        if result.scalar_one_or_none() is None:
            raise JobLeaseLostException()

    async def set_progress(self, job_id: UUID, claim_token: UUID, progress: int) -> None:
        if progress < 0 or progress > 99:
            raise ValueError("Running job progress must be between 0 and 99")
        now = await self._database_now()
        result = await self._session.execute(
            update(IngestionJobModel)
            .where(
                IngestionJobModel.id == job_id,
                IngestionJobModel.state == "running",
                IngestionJobModel.claim_token == claim_token,
                IngestionJobModel.cancellation_requested.is_(False),
                IngestionJobModel.lease_expires_at > now,
            )
            .values(progress=progress, updated_at=now)
            .returning(IngestionJobModel.id)
        )
        if result.scalar_one_or_none() is None:
            raise JobLeaseLostException()

    async def complete(self, job_id: UUID, claim_token: UUID) -> None:
        now = await self._database_now()
        result = await self._session.execute(
            update(IngestionJobModel)
            .where(
                IngestionJobModel.id == job_id,
                IngestionJobModel.state == "running",
                IngestionJobModel.claim_token == claim_token,
                IngestionJobModel.cancellation_requested.is_(False),
                IngestionJobModel.lease_expires_at > now,
            )
            .values(
                state="succeeded",
                progress=100,
                lease_owner=None,
                lease_expires_at=None,
                claim_token=None,
                finished_at=now,
                updated_at=now,
            )
            .returning(IngestionJobModel.id)
        )
        if result.scalar_one_or_none() is None:
            raise JobLeaseLostException()

    async def fail(
        self,
        job_id: UUID,
        claim_token: UUID,
        *,
        error_code: str,
        error_detail: str | None = None,
        retryable: bool,
        base_delay_seconds: int = 5,
        max_delay_seconds: int = 300,
    ) -> str:
        job = await self._locked_claim(job_id, claim_token)
        now = await self._database_now()
        should_retry = retryable and job.attempt_count < job.max_attempts and not job.cancellation_requested
        job.last_error_code = error_code[:64]
        job.last_error_detail = error_detail[:1000] if error_detail else None
        job.lease_owner = None
        job.lease_expires_at = None
        job.claim_token = None
        job.updated_at = now
        if should_retry:
            exponent = max(job.attempt_count - 1, 0)
            delay = min(base_delay_seconds * (2 ** exponent), max_delay_seconds)
            jitter = random.SystemRandom().uniform(0, min(delay * 0.2, 30))
            job.state = "retry_wait"
            job.next_attempt_at = now + timedelta(seconds=delay + jitter)
            job.finished_at = None
        else:
            job.state = "cancelled" if job.cancellation_requested else "failed"
            job.finished_at = now
        await self._session.flush()
        return job.state

    async def request_cancellation(self, job_id: UUID) -> str:
        job = await self._session.scalar(
            select(IngestionJobModel).where(IngestionJobModel.id == job_id).with_for_update()
        )
        if job is None:
            raise ItemNotFoundException()
        if job.state in {"succeeded", "failed", "cancelled"}:
            return job.state
        now = await self._database_now()
        job.cancellation_requested = True
        job.retry_requested = False
        job.updated_at = now
        await self._session.flush()
        return "cancellation_requested"

    async def request_retry(self, job_id: UUID) -> str:
        job = await self._session.scalar(
            select(IngestionJobModel)
            .where(IngestionJobModel.id == job_id)
            .with_for_update()
        )
        if job is None:
            raise ItemNotFoundException()
        if job.state == "succeeded":
            return job.state
        if job.state not in {"failed", "cancelled"}:
            raise ConcurrencyConflictException("Only a terminal ingestion job can be retried.")
        revision = await self._session.scalar(
            select(DocumentRevisionModel)
            .where(DocumentRevisionModel.id == job.document_revision_id)
            .with_for_update()
        )
        if revision is None:
            raise ItemNotFoundException()
        if revision.status == "quarantined":
            raise ConcurrencyConflictException("A quarantined revision requires storage repair before retry.")
        if revision.storage_key is None:
            raise ConcurrencyConflictException("The revision has no finalized storage object.")
        now = await self._database_now()
        job.retry_requested = True
        job.cancellation_requested = False
        job.updated_at = now
        await self._session.flush()
        return "retry_requested"

    async def is_cancellation_requested(self, job_id: UUID, claim_token: UUID) -> bool:
        now = await self._database_now()
        value = await self._session.scalar(
            select(IngestionJobModel.cancellation_requested).where(
                IngestionJobModel.id == job_id,
                IngestionJobModel.state == "running",
                IngestionJobModel.claim_token == claim_token,
                IngestionJobModel.lease_expires_at > now,
            )
        )
        if value is None:
            raise JobLeaseLostException()
        return value

    async def acknowledge_cancellation(self, job_id: UUID, claim_token: UUID) -> None:
        now = await self._database_now()
        result = await self._session.execute(
            update(IngestionJobModel)
            .where(
                IngestionJobModel.id == job_id,
                IngestionJobModel.state == "running",
                IngestionJobModel.claim_token == claim_token,
                IngestionJobModel.cancellation_requested.is_(True),
                IngestionJobModel.lease_expires_at > now,
            )
            .values(
                state="cancelled",
                lease_owner=None,
                lease_expires_at=None,
                claim_token=None,
                finished_at=now,
                updated_at=now,
            )
            .returning(IngestionJobModel.id)
        )
        if result.scalar_one_or_none() is None:
            raise JobLeaseLostException()
        revision_id = select(IngestionJobModel.document_revision_id).where(
            IngestionJobModel.id == job_id
        )
        await self._session.execute(
            update(DocumentRevisionModel)
            .where(
                DocumentRevisionModel.id == revision_id.scalar_subquery(),
                DocumentRevisionModel.status.in_(("pending", "processing", "ready")),
            )
            .values(status="cancelled", failure_code="cancelled")
        )

    async def _locked_claim(self, job_id: UUID, claim_token: UUID) -> IngestionJobModel:
        now = await self._database_now()
        job = await self._session.scalar(
            select(IngestionJobModel)
            .where(
                IngestionJobModel.id == job_id,
                IngestionJobModel.state == "running",
                IngestionJobModel.claim_token == claim_token,
                IngestionJobModel.lease_expires_at > now,
            )
            .with_for_update()
        )
        if job is None:
            raise JobLeaseLostException()
        return job

    async def _database_now(self):
        return await self._session.scalar(select(func.now()))

    async def _finalize_exhausted(self, now) -> None:
        exhausted = or_(
            and_(
                IngestionJobModel.state.in_(("queued", "retry_wait")),
                IngestionJobModel.next_attempt_at <= now,
            ),
            and_(
                IngestionJobModel.state == "running",
                IngestionJobModel.lease_expires_at <= now,
            ),
        )
        result = await self._session.execute(
            update(IngestionJobModel)
            .where(
                exhausted,
                IngestionJobModel.attempt_count >= IngestionJobModel.max_attempts,
            )
            .values(
                state="failed",
                last_error_code="attempts_exhausted",
                last_error_detail=None,
                lease_owner=None,
                lease_expires_at=None,
                claim_token=None,
                finished_at=now,
                updated_at=now,
            )
            .returning(IngestionJobModel.document_revision_id)
        )
        revision_ids = tuple(result.scalars())
        if revision_ids:
            await self._session.execute(
                update(DocumentRevisionModel)
                .where(
                    DocumentRevisionModel.id.in_(revision_ids),
                    DocumentRevisionModel.status.in_(("pending", "processing", "ready")),
                )
                .values(status="failed", failure_code="attempts_exhausted")
            )

    async def _activate_retry_requests(self, now) -> None:
        jobs = tuple(
            await self._session.scalars(
                select(IngestionJobModel)
                .where(
                    IngestionJobModel.state.in_(("failed", "cancelled")),
                    IngestionJobModel.retry_requested.is_(True),
                    IngestionJobModel.cancellation_requested.is_(False),
                )
                .order_by(IngestionJobModel.updated_at, IngestionJobModel.id)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
        )
        for job in jobs:
            revision = await self._session.scalar(
                select(DocumentRevisionModel)
                .where(DocumentRevisionModel.id == job.document_revision_id)
                .with_for_update()
            )
            if revision is None or revision.status == "quarantined" or revision.storage_key is None:
                job.retry_requested = False
                job.last_error_code = "retry_precondition_failed"
                job.updated_at = now
                continue
            job.state = "queued"
            job.progress = 0
            job.attempt_count = 0
            job.next_attempt_at = now
            job.retry_requested = False
            job.last_error_code = None
            job.last_error_detail = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.claim_token = None
            job.started_at = None
            job.finished_at = None
            job.updated_at = now
            revision.status = "pending"
            revision.failure_code = None

    async def _finalize_cancellation_requests(self, now) -> None:
        result = await self._session.execute(
            update(IngestionJobModel)
            .where(
                or_(
                    IngestionJobModel.state.in_(("queued", "retry_wait")),
                    and_(
                        IngestionJobModel.state == "running",
                        IngestionJobModel.lease_expires_at <= now,
                    ),
                ),
                IngestionJobModel.cancellation_requested.is_(True),
            )
            .values(
                state="cancelled",
                retry_requested=False,
                lease_owner=None,
                lease_expires_at=None,
                claim_token=None,
                finished_at=now,
                updated_at=now,
            )
            .returning(IngestionJobModel.document_revision_id)
        )
        revision_ids = tuple(result.scalars())
        if revision_ids:
            await self._session.execute(
                update(DocumentRevisionModel)
                .where(
                    DocumentRevisionModel.id.in_(revision_ids),
                    DocumentRevisionModel.status.in_(("pending", "processing", "ready")),
                )
                .values(status="cancelled", failure_code="cancelled")
            )
