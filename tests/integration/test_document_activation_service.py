from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from src.gateway.application.services.document_activation_service import ActivationChunk, DocumentActivationService
from src.gateway.application.services.document_upload_service import DocumentUploadService
from src.gateway.application.services.ingestion_job_service import IngestionJobService
from src.gateway.domain.exceptions import JobLeaseLostException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionChunkModel, DocumentRevisionModel, EmbeddingGenerationModel, IngestionJobModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from tests.e2e.harness.test_env import TestEnvironment


async def _chunks(value: bytes):
    yield value


def _principal() -> Principal:
    import hashlib

    digest = hashlib.sha256(b"sk-test-admin").digest()
    return Principal(
        subject_id=str(UUID(bytes=digest[:16])),
        kind=PrincipalKind.COMPATIBILITY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:write"}),
    )


async def test_activation_is_atomic_fenced_and_makes_revision_searchable(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        receipt = await DocumentUploadService(
            env.session_factory,
            LocalVersionedObjectStorage(tmp_path),
            max_upload_bytes=1024,
        ).upload_new(
            principal=_principal(),
            space_id="test_ws",
            display_name="Activation",
            original_filename="activation.txt",
            mime_type="text/plain",
            chunks=_chunks(b"alpha beta"),
            idempotency_key="activation",
        )
        generation_id = uuid4()
        async with env.session_factory.begin() as session:
            session.add(
                EmbeddingGenerationModel(
                    id=generation_id,
                    purpose="retrieval",
                    model_id="test-embedding",
                    dimensions=EMBED_DIM,
                    status="active",
                )
            )
        async with env.session_factory.begin() as session:
            claim = await IngestionJobService(session).claim_next("worker-a", lease_seconds=60)

        activation_chunks = [
            ActivationChunk(
                content="alpha",
                content_hash="a" * 64,
                embedding=[1.0] + [0.0] * (EMBED_DIM - 1),
                language="en",
                parser_metadata={"page": 1},
            ),
            ActivationChunk(
                content="beta",
                content_hash="b" * 64,
                embedding=[0.0, 1.0] + [0.0] * (EMBED_DIM - 2),
                language="en",
                parser_metadata={"page": 2},
            ),
        ]
        async with env.session_factory.begin() as session:
            await DocumentActivationService(session).activate(
                claim,
                parser_version="text-v1",
                embedding_generation_id=generation_id,
                chunks=activation_chunks,
            )

        async with env.session_factory() as session:
            document = await session.get(DocumentModel, receipt.document_id)
            revision = await session.get(DocumentRevisionModel, receipt.revision_id)
            job = await session.get(IngestionJobModel, receipt.job_id)
            assert document.current_revision_id == receipt.revision_id
            assert revision.status == "active"
            assert revision.ready_at is not None
            assert revision.activated_at is not None
            assert job.state == "succeeded"
            assert await session.scalar(select(func.count()).select_from(DocumentRevisionChunkModel)) == 2
            assert await session.scalar(
                select(func.count()).select_from(RetrievalUnitModel).where(RetrievalUnitModel.active.is_(True))
            ) == 2

        async with env.session_factory.begin() as session:
            with pytest.raises(JobLeaseLostException):
                await DocumentActivationService(session).activate(
                    claim,
                    parser_version="text-v1",
                    embedding_generation_id=generation_id,
                    chunks=activation_chunks,
                )
