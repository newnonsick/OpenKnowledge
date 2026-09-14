from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.audit_service import AuditService
from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.services.idempotency_service import IdempotencyService, ReservationStatus
from src.gateway.application.services.ingestion_job_service import IngestionJobService
from src.gateway.application.use_cases.context import UseCaseContext, UseCaseOutcome, actor_id, require_mutation_tools
from src.gateway.domain.authorization import Action
from src.gateway.domain.exceptions import AuthorizationException, ValidationException
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.ingestion_models import IngestionJobModel


@dataclass(frozen=True, slots=True)
class ListIngestionJobsQuery:
    space_id: str | None = None
    state: str | None = None
    page: int = 1
    page_size: int = 50


@dataclass(frozen=True, slots=True)
class GetIngestionJobQuery:
    job_id: UUID


@dataclass(frozen=True, slots=True)
class MutateIngestionJobCommand:
    job_id: UUID
    operation: Literal["cancel", "retry"]


def job_payload(job: IngestionJobModel) -> dict:
    return {
        "id": str(job.id),
        "space_id": job.space_id,
        "job_type": job.job_type,
        "document_id": str(job.document_id) if job.document_id is not None else None,
        "document_revision_id": str(job.document_revision_id) if job.document_revision_id is not None else None,
        "knowledge_item_id": str(job.knowledge_item_id) if job.knowledge_item_id is not None else None,
        "knowledge_revision_id": str(job.knowledge_revision_id) if job.knowledge_revision_id is not None else None,
        "state": job.state,
        "progress": job.progress,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "last_error_code": job.last_error_code,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


class IngestionUseCases:
    def __init__(
        self,
        session: AsyncSession,
        *,
        job_service_factory=None,
        idempotency_factory=None,
    ) -> None:
        self._session = session
        self._job_service_factory = job_service_factory or IngestionJobService
        self._idempotency_factory = idempotency_factory or IdempotencyService

    async def scoped_spaces(self, ctx: UseCaseContext, space_id: str | None) -> tuple[str, ...]:
        effective = await AuthorizationService(self._session).effective_space_ids(
            actor_id(ctx.principal), principal=ctx.principal
        )
        if space_id is not None:
            if space_id not in effective:
                return ()
            return (space_id,)
        return effective

    async def list_jobs(
        self, ctx: UseCaseContext, query: ListIngestionJobsQuery
    ) -> tuple[list[dict], dict[str, int]]:
        scoped = await self.scoped_spaces(ctx, query.space_id)
        metadata = self._page_metadata(page=query.page, page_size=query.page_size, total_items=0)
        if not scoped:
            return [], metadata
        base = (
            select(IngestionJobModel)
            .where(IngestionJobModel.space_id.in_(scoped))
            .order_by(IngestionJobModel.created_at.desc(), IngestionJobModel.id.desc())
        )
        if query.state is not None:
            base = base.where(IngestionJobModel.state == query.state)
        jobs, metadata = await self._paginate(base, page=query.page, page_size=query.page_size)
        return [job_payload(job) for job in jobs], metadata

    async def get_job(self, ctx: UseCaseContext, query: GetIngestionJobQuery) -> dict:
        job = await self._session.get(IngestionJobModel, query.job_id)
        if job is None:
            raise AuthorizationException()
        await AuthorizationService(self._session).authorize_space(
            ctx.principal, job.space_id, Action.CONTENT_READ
        )
        return job_payload(job)

    async def mutate_job(
        self, ctx: UseCaseContext, command: MutateIngestionJobCommand
    ) -> UseCaseOutcome[dict]:
        require_mutation_tools(ctx.policy)
        if not ctx.idempotency_key:
            raise ValidationException("An idempotency key is required.")
        job = await self._session.get(IngestionJobModel, command.job_id)
        if job is None:
            raise AuthorizationException()
        await AuthorizationService(self._session).authorize_space(
            ctx.principal, job.space_id, Action.CONTENT_WRITE
        )
        reservation = await self._idempotency_factory(self._session).reserve(
            actor_id=ctx.principal.subject_id,
            operation=f"ingestion_job.{command.operation}",
            idempotency_key=ctx.idempotency_key,
            payload={"job_id": str(command.job_id)},
        )
        if reservation.status is ReservationStatus.IN_PROGRESS:
            from src.gateway.domain.exceptions import ResourceConflictException

            raise ResourceConflictException("An identical request is still in progress.")
        if reservation.status is ReservationStatus.REPLAY:
            if command.operation == "cancel" and job.cancellation_requested and job.state not in {
                "failed",
                "cancelled",
                "succeeded",
            }:
                state = "cancellation_requested"
            elif command.operation == "retry" and job.retry_requested:
                state = "retry_requested"
            else:
                state = job.state
        elif command.operation == "cancel":
            state = await self._job_service_factory(self._session).request_cancellation(command.job_id)
        else:
            state = await self._job_service_factory(self._session).request_retry(command.job_id)
        if reservation.status is not ReservationStatus.REPLAY:
            AuditService(AuditRepository(self._session)).record(
                actor_member_id=actor_id(ctx.principal),
                actor_kind=ctx.principal.kind.value,
                request_id=ctx.request_id,
                action=f"ingestion_job.{command.operation}_requested",
                resource_type="ingestion_job",
                resource_id=str(command.job_id),
                details={"space_id": job.space_id, "state": state},
            )
            await self._idempotency_factory(self._session).complete(
                reservation.record_id,
                response_status=200,
                resource_ids=[str(command.job_id)],
            )
        return UseCaseOutcome(
            {"id": str(command.job_id), "state": state},
            reservation.status is ReservationStatus.REPLAY,
        )

    async def _paginate(self, query, *, page: int, page_size: int):
        from sqlalchemy import func

        count_query = select(func.count()).select_from(query.order_by(None).subquery())
        total_items = int(await self._session.scalar(count_query) or 0)
        metadata = self._page_metadata(page=page, page_size=page_size, total_items=total_items)
        if metadata["total_pages"] == 0:
            return [], metadata
        page_query = query.offset((metadata["page"] - 1) * page_size).limit(page_size)
        items = list(await self._session.scalars(page_query))
        return items, metadata

    @staticmethod
    def _page_metadata(*, page: int, page_size: int, total_items: int) -> dict[str, int]:
        total_pages = (total_items + page_size - 1) // page_size if total_items else 0
        return {
            "page": min(page, total_pages) if total_pages else 1,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
        }
