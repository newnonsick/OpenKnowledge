from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.application.services.embedding_generation_service import (
    EmbeddingGenerationService,
)
from src.gateway.config import get_settings
from src.gateway.infrastructure.persistence.ingestion_models import (
    EmbeddingGenerationModel,
    MigrationBackfillRunModel,
    RetrievalUnitModel,
)
from src.gateway.infrastructure.persistence.models import DocumentChunk, KnowledgeRevision


@dataclass(frozen=True)
class ReembedTarget:
    name: str
    model: Any
    generation_aware: bool


REEMBED_TARGETS: tuple[ReembedTarget, ...] = (
    ReembedTarget("knowledge_revisions", KnowledgeRevision, generation_aware=False),
    ReembedTarget("document_chunks", DocumentChunk, generation_aware=False),
    ReembedTarget("retrieval_units", RetrievalUnitModel, generation_aware=True),
)


def _run_name(target: ReembedTarget) -> str:
    return f"embedding_reembed:{target.name}"


_DEACTIVATE_SUPERSEDED_UNITS_SQL = (
    "UPDATE retrieval_units AS stale "
    "SET active = false, deactivated_at = :deactivated_at "
    "WHERE stale.id = ANY(CAST(:ids AS uuid[])) "
    "AND stale.active "
    "AND EXISTS ("
    "SELECT 1 FROM retrieval_units AS twin "
    "WHERE twin.active "
    "AND twin.id <> stale.id "
    "AND twin.embedding_generation_id = CAST(:generation_id AS uuid) "
    "AND twin.source_type = stale.source_type "
    "AND twin.knowledge_revision_id IS NOT DISTINCT FROM stale.knowledge_revision_id "
    "AND twin.document_revision_chunk_id IS NOT DISTINCT FROM stale.document_revision_chunk_id"
    ")"
)


@dataclass(frozen=True)
class ReembedProgress:
    table: str
    phase: str
    rows_migrated: int
    completed: bool
    last_error_code: Optional[str] = None


@dataclass(frozen=True)
class ReembedBatch:
    table: str
    rows_embedded: int
    completed: bool


class EmbeddingReembedService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_client: IEmbeddingClient,
        generation_service: EmbeddingGenerationService,
        *,
        batch_size: Optional[int] = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_client = embedding_client
        self._generation_service = generation_service
        resolved_batch_size = (
            batch_size
            if batch_size is not None
            else get_settings().gateway.embedding_reembed_batch_size
        )
        if resolved_batch_size < 1:
            raise ValueError("Embedding re-embed batch size must be at least 1")
        self._batch_size = resolved_batch_size

    async def status(self) -> tuple[ReembedProgress, ...]:
        async with self._session_factory() as session:
            runs = (
                await session.scalars(
                    select(MigrationBackfillRunModel).where(
                        MigrationBackfillRunModel.name.in_(
                            [_run_name(target) for target in REEMBED_TARGETS]
                        )
                    )
                )
            ).all()
        by_name = {run.name: run for run in runs}
        return tuple(
            ReembedProgress(
                table=target.name,
                phase=run.phase,
                rows_migrated=run.rows_migrated,
                completed=run.phase == "complete",
                last_error_code=run.last_error_code,
            )
            for target in REEMBED_TARGETS
            if (run := by_name.get(_run_name(target))) is not None
        )

    async def enqueue(self) -> tuple[ReembedProgress, ...]:
        await self._generation_service.ensure_active(
            model_id=get_settings().embedding.model_id,
            dimensions=get_settings().embedding.dimension,
        )
        async with self._session_factory.begin() as session:
            for target in REEMBED_TARGETS:
                name = _run_name(target)
                run = await session.get(MigrationBackfillRunModel, name)
                high_water_id = await session.scalar(select(func.max(target.model.id)))
                if run is None:
                    session.add(
                        MigrationBackfillRunModel(
                            name=name,
                            phase="snapshot",
                            high_water_id=high_water_id,
                            cursor_id=None,
                        )
                    )
                    continue
                if run.phase != "complete":
                    continue
                run.phase = "snapshot"
                run.high_water_id = high_water_id
                run.cursor_id = None
                run.rows_migrated = 0
                run.completed_at = None
                run.last_error_code = None
                run.updated_at = await session.scalar(select(func.now()))
        return await self.status()

    async def run_batch(self, batch_size: Optional[int] = None) -> ReembedBatch:
        size = batch_size or self._batch_size
        for target in REEMBED_TARGETS:
            async with self._session_factory() as session:
                run = await session.get(MigrationBackfillRunModel, _run_name(target))
                if run is None or run.phase == "complete":
                    continue
                rows = await self._pending_rows(session, target, run, size)
                if not rows:
                    await self._complete(session, run)
                    continue
                generation_id = (
                    await self._active_generation_id() if target.generation_aware else None
                )
                try:
                    vectors = await self._embedding_client.embed_texts(
                        [row[1] for row in rows]
                    )
                except Exception as exc:
                    run.last_error_code = type(exc).__name__
                    run.updated_at = await session.scalar(select(func.now()))
                    await session.commit()
                    raise
                if len(vectors) != len(rows):
                    raise RuntimeError(
                        f"Embedding provider returned {len(vectors)} vectors for "
                        f"{len(rows)} rows while re-embedding {target.name}."
                    )
                await self._write_embeddings(session, target, rows, vectors, generation_id)
                run.cursor_id = rows[-1][0]
                run.rows_migrated += len(rows)
                run.last_error_code = None
                run.updated_at = await session.scalar(select(func.now()))
                await session.commit()
                return ReembedBatch(
                    table=target.name,
                    rows_embedded=len(rows),
                    completed=False,
                )
        return ReembedBatch(table="", rows_embedded=0, completed=True)

    async def _pending_rows(
        self,
        session: AsyncSession,
        target: ReembedTarget,
        run: MigrationBackfillRunModel,
        size: int,
    ) -> list[Any]:
        model = target.model
        statement = (
            select(model.id, model.content)
            .where(model.embedding.is_(None))
            .order_by(model.id)
            .limit(size)
        )
        if run.high_water_id is not None:
            statement = statement.where(model.id <= run.high_water_id)
        if run.cursor_id is not None:
            statement = statement.where(model.id > run.cursor_id)
        if target.generation_aware:
            statement = statement.where(model.active.is_(True))
        return list((await session.execute(statement)).all())

    async def _write_embeddings(
        self,
        session: AsyncSession,
        target: ReembedTarget,
        rows: list[Any],
        vectors: list[list[float]],
        generation_id: Optional[UUID],
    ) -> None:
        if not target.generation_aware or generation_id is None:
            for row, vector in zip(rows, vectors):
                await session.execute(
                    update(target.model)
                    .where(target.model.id == row[0])
                    .values(embedding=vector)
                )
            return

        await session.execute(
            text(_DEACTIVATE_SUPERSEDED_UNITS_SQL),
            {
                "ids": [row[0] for row in rows],
                "generation_id": generation_id,
                "deactivated_at": datetime.now(timezone.utc),
            },
        )
        for row, vector in zip(rows, vectors):
            await session.execute(
                update(RetrievalUnitModel)
                .where(
                    RetrievalUnitModel.id == row[0],
                    RetrievalUnitModel.active.is_(True),
                )
                .values(embedding=vector, embedding_generation_id=generation_id)
            )

    async def _active_generation_id(self) -> UUID:
        async with self._session_factory() as session:
            generation_id = await session.scalar(
                select(EmbeddingGenerationModel.id).where(
                    EmbeddingGenerationModel.purpose == "retrieval",
                    EmbeddingGenerationModel.status == "active",
                )
            )
        if generation_id is None:
            raise RuntimeError(
                "No active embedding generation; run "
                "'python -m src.gateway.cli ensure-embedding-generation' first."
            )
        return generation_id

    async def _complete(
        self,
        session: AsyncSession,
        run: MigrationBackfillRunModel,
    ) -> None:
        run.phase = "complete"
        run.completed_at = await session.scalar(select(func.now()))
        run.updated_at = run.completed_at
        await session.commit()
