from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.embedding_reembed_service import REEMBED_TARGETS
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel


REINDEX_PURPOSES = ("retrieval",)


@dataclass(frozen=True, slots=True)
class GenerationSummary:
    id: UUID
    purpose: str
    model_id: str
    dimensions: int
    status: str
    created_at: datetime | None
    activated_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReindexTargetStatus:
    name: str
    phase: str | None
    rows_migrated: int
    completed: bool
    pending: bool


@dataclass(frozen=True, slots=True)
class PromoteOutcome:
    promoted: GenerationSummary
    retired_id: UUID | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class RollbackOutcome:
    activated: GenerationSummary
    retired_id: UUID | None
    replayed: bool


def _generation_payload(generation: EmbeddingGenerationModel) -> dict:
    return {
        "id": str(generation.id),
        "purpose": generation.purpose,
        "model_id": generation.model_id,
        "dimensions": generation.dimensions,
        "status": generation.status,
        "created_at": generation.created_at.isoformat() if generation.created_at else None,
        "activated_at": generation.activated_at.isoformat() if generation.activated_at else None,
    }


def _summarize(generation: EmbeddingGenerationModel) -> GenerationSummary:
    return GenerationSummary(
        id=generation.id,
        purpose=generation.purpose,
        model_id=generation.model_id,
        dimensions=generation.dimensions,
        status=generation.status,
        created_at=generation.created_at,
        activated_at=generation.activated_at,
    )


class EmbeddingGenerationLifecycleService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_generations(self) -> list[GenerationSummary]:
        rows = list(
            await self._session.scalars(
                select(EmbeddingGenerationModel)
                .where(EmbeddingGenerationModel.purpose.in_(REINDEX_PURPOSES))
                .order_by(EmbeddingGenerationModel.created_at.desc(), EmbeddingGenerationModel.id.desc())
            )
        )
        return [_summarize(row) for row in rows]

    async def active_generation(self) -> GenerationSummary | None:
        row = await self._session.scalar(
            select(EmbeddingGenerationModel).where(
                EmbeddingGenerationModel.purpose == "retrieval",
                EmbeddingGenerationModel.status == "active",
            )
        )
        return _summarize(row) if row is not None else None

    async def reembed_progress(self) -> list[ReindexTargetStatus]:
        from src.gateway.infrastructure.persistence.ingestion_models import MigrationBackfillRunModel

        names = [f"embedding_reembed:{target.name}" for target in REEMBED_TARGETS]
        runs = {
            run.name: run
            for run in await self._session.scalars(
                select(MigrationBackfillRunModel).where(MigrationBackfillRunModel.name.in_(names))
            )
        }
        statuses: list[ReindexTargetStatus] = []
        for target in REEMBED_TARGETS:
            run = runs.get(f"embedding_reembed:{target.name}")
            if run is None:
                statuses.append(
                    ReindexTargetStatus(
                        name=target.name,
                        phase=None,
                        rows_migrated=0,
                        completed=True,
                        pending=False,
                    )
                )
                continue
            completed = run.phase == "complete"
            statuses.append(
                ReindexTargetStatus(
                    name=target.name,
                    phase=run.phase,
                    rows_migrated=int(run.rows_migrated or 0),
                    completed=completed,
                    pending=not completed,
                )
            )
        return statuses

    async def pending_targets(self) -> tuple[str, ...]:
        return tuple(status.name for status in await self.reembed_progress() if status.pending)

    async def status_payload(self) -> dict:
        generations = await self.list_generations()
        active = next((generation for generation in generations if generation.status == "active"), None)
        targets = await self.reembed_progress()
        pending = tuple(target.name for target in targets if target.pending)
        return {
            "purpose": "retrieval",
            "active_generation_id": str(active.id) if active is not None else None,
            "generations": [
                {
                    "id": str(generation.id),
                    "purpose": generation.purpose,
                    "model_id": generation.model_id,
                    "dimensions": generation.dimensions,
                    "status": generation.status,
                    "created_at": generation.created_at.isoformat() if generation.created_at else None,
                    "activated_at": generation.activated_at.isoformat() if generation.activated_at else None,
                }
                for generation in generations
            ],
            "targets": [
                {
                    "name": target.name,
                    "phase": target.phase,
                    "rows_migrated": target.rows_migrated,
                    "completed": target.completed,
                    "pending": target.pending,
                }
                for target in targets
            ],
            "pending_targets": list(pending),
        }

    async def promote(
        self,
        generation_id: UUID,
        *,
        force: bool,
        now: datetime | None = None,
    ) -> PromoteOutcome:
        target = await self._session.get(EmbeddingGenerationModel, generation_id)
        if target is None or target.purpose != "retrieval" or target.status != "building":
            raise AuthorizationException()
        pending = await self.pending_targets()
        if pending and not force:
            raise ResourceConflictException(
                "Re-embedding is still pending for this generation.",
                details={"pending_targets": list(pending), "generation_id": str(generation_id)},
            )
        current = await self._session.scalar(
            select(EmbeddingGenerationModel).where(
                EmbeddingGenerationModel.purpose == "retrieval",
                EmbeddingGenerationModel.status == "active",
            )
        )
        if current is not None and current.id == target.id:
            return PromoteOutcome(promoted=_summarize(target), retired_id=None, replayed=True)
        observed = now or datetime.now(timezone.utc)
        retired_id: UUID | None = None
        if current is not None:
            await self._session.execute(
                update(EmbeddingGenerationModel)
                .where(EmbeddingGenerationModel.id == current.id)
                .values(status="retired")
            )
            retired_id = current.id
        target.status = "active"
        target.activated_at = observed
        await self._session.flush()
        await self._session.refresh(target)
        return PromoteOutcome(promoted=_summarize(target), retired_id=retired_id, replayed=False)

    async def rollback(self, *, now: datetime | None = None) -> RollbackOutcome:
        current = await self._session.scalar(
            select(EmbeddingGenerationModel).where(
                EmbeddingGenerationModel.purpose == "retrieval",
                EmbeddingGenerationModel.status == "active",
            )
        )
        if current is None:
            raise AuthorizationException()
        candidate = await self._session.scalar(
            select(EmbeddingGenerationModel)
            .where(
                EmbeddingGenerationModel.purpose == "retrieval",
                EmbeddingGenerationModel.status == "retired",
            )
            .order_by(
                EmbeddingGenerationModel.activated_at.desc().nullslast(),
                EmbeddingGenerationModel.created_at.desc(),
                EmbeddingGenerationModel.id.desc(),
            )
        )
        if candidate is None:
            raise AuthorizationException()
        if candidate.id == current.id:
            return RollbackOutcome(activated=_summarize(current), retired_id=None, replayed=True)
        observed = now or datetime.now(timezone.utc)
        await self._session.execute(
            update(EmbeddingGenerationModel)
            .where(EmbeddingGenerationModel.id == current.id)
            .values(status="retired")
        )
        candidate.status = "active"
        candidate.activated_at = observed
        await self._session.flush()
        await self._session.refresh(candidate)
        return RollbackOutcome(activated=_summarize(candidate), retired_id=current.id, replayed=False)

    async def stale_knowledge_count(self, *, space_ids: tuple[str, ...], cutoff: datetime) -> int:
        from src.gateway.infrastructure.persistence.models import KnowledgeItem as KnowledgeItemModel

        if not space_ids:
            return 0
        return int(
            await self._session.scalar(
                select(func.count(KnowledgeItemModel.id)).where(
                    KnowledgeItemModel.is_deleted.is_(False),
                    KnowledgeItemModel.archived_at.is_(None),
                    KnowledgeItemModel.workspace_id.in_(space_ids),
                    KnowledgeItemModel.updated_at < cutoff,
                )
            )
            or 0
        )
