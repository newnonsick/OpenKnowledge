from sqlalchemy import select

from src.gateway.application.services.embedding_generation_service import (
    EmbeddingGenerationService,
)
from src.gateway.infrastructure.persistence.ingestion_models import (
    EmbeddingGenerationModel,
)
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_embedding_generation_is_idempotent_and_rotates_model_changes() -> None:
    async with isolated_postgres_database() as (_, session_factory):
        service = EmbeddingGenerationService(session_factory)

        created = await service.ensure_active(model_id="embed-a", dimensions=1024)
        assert created.status == "active"
        assert created.model_id == "embed-a"
        assert created.dimensions == 1024
        assert created.activated_at is not None

        reused = await service.ensure_active(model_id="embed-a", dimensions=1024)
        assert reused.id == created.id

        replacement = await service.ensure_active(model_id="embed-b", dimensions=1024)
        assert replacement.id != created.id
        assert replacement.status == "active"

        async with session_factory() as session:
            rows = (
                await session.scalars(
                    select(EmbeddingGenerationModel).order_by(
                        EmbeddingGenerationModel.created_at
                    )
                )
            ).all()

        assert [(row.status, row.model_id) for row in rows] == [
            ("retired", "embed-a"),
            ("active", "embed-b"),
        ]
