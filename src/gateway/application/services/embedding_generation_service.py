from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.gateway.infrastructure.persistence.ingestion_models import (
    EmbeddingGenerationModel,
)


class EmbeddingGenerationService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ensure_active(self, *, model_id: str, dimensions: int) -> EmbeddingGenerationModel:
        async with self._session_factory.begin() as session:
            active = await session.scalar(
                select(EmbeddingGenerationModel).where(
                    EmbeddingGenerationModel.purpose == "retrieval",
                    EmbeddingGenerationModel.status == "active",
                )
            )
            if active is not None:
                if active.model_id == model_id and active.dimensions == dimensions:
                    return active
                await session.execute(
                    update(EmbeddingGenerationModel)
                    .where(EmbeddingGenerationModel.id == active.id)
                    .values(status="retired")
                )

            now = datetime.now(timezone.utc)
            generation = EmbeddingGenerationModel(
                purpose="retrieval",
                model_id=model_id,
                dimensions=dimensions,
                status="active",
                activated_at=now,
            )
            session.add(generation)
            await session.flush()
            await session.refresh(generation)
            return generation
