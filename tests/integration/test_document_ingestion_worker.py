from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update

from src.gateway.application.services.bounded_document_parser import ParsedDocument
from src.gateway.application.services.document_ingestion_worker import DocumentIngestionWorker
from src.gateway.application.services.document_upload_service import DocumentUploadService
from src.gateway.domain.exceptions import ConcurrencyConflictException, EmbeddingException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionModel, EmbeddingGenerationModel, IngestionJobModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, Workspace
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from src.gateway.observability import metrics_registry_context
from src.gateway.presentation.metrics import MetricsRegistry
from tests.e2e.harness.test_env import TestEnvironment
from tests.integration.postgres_test_database import isolated_postgres_database


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


class _Parser:
    async def parse(self, *, filename: str, mime_type: str, content: bytes) -> ParsedDocument:
        return ParsedDocument(text=content.decode(), parser_version="text-test-v1")


class _EmbeddingClient:
    dimension = EMBED_DIM

    async def embed_texts(self, texts):
        return [[1.0] + [0.0] * (EMBED_DIM - 1) for _ in texts]


class _FailOnceEmbeddingClient(_EmbeddingClient):
    def __init__(self) -> None:
        self.failed = False

    async def embed_texts(self, texts):
        if not self.failed:
            self.failed = True
            raise EmbeddingException()
        return await super().embed_texts(texts)


async def _upload(env, storage, idempotency_key: str):
    return await DocumentUploadService(
        env.session_factory,
        storage,
        max_upload_bytes=1024,
    ).upload_new(
        principal=_principal(),
        space_id="test_ws",
        display_name="Worker document",
        original_filename="worker.txt",
        mime_type="text/plain",
        chunks=_chunks(b"alpha beta gamma"),
        idempotency_key=idempotency_key,
    )


async def _generation(env):
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
    return generation_id


async def test_worker_parses_embeds_and_atomically_activates_revision(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        storage = LocalVersionedObjectStorage(tmp_path)
        receipt = await _upload(env, storage, "worker-success")
        await _generation(env)
        worker = DocumentIngestionWorker(
            env.session_factory,
            storage,
            _Parser(),
            _EmbeddingClient(),
            worker_id="worker-success",
            lease_seconds=30,
            heartbeat_interval_seconds=1,
            chunk_size=8,
            chunk_overlap=2,
            max_chunks=20,
        )

        registry = MetricsRegistry()
        metrics_token = metrics_registry_context.set(registry)
        try:
            assert await worker.run_once() == receipt.job_id
        finally:
            metrics_registry_context.reset(metrics_token)

        metrics = registry.render()
        assert 'gateway_ingestion_events_total{event="claim",outcome="success"} 1' in metrics
        assert 'gateway_ingestion_events_total{event="terminal",outcome="succeeded"} 1' in metrics
        assert 'gateway_ingestion_processing_duration_seconds_count{outcome="succeeded"} 1' in metrics
        assert 'gateway_ingestion_queue_depth{state="queued"} 1' in metrics

        async with env.session_factory() as session:
            document = await session.get(DocumentModel, receipt.document_id)
            revision = await session.get(DocumentRevisionModel, receipt.revision_id)
            job = await session.get(IngestionJobModel, receipt.job_id)
            assert document.current_revision_id == receipt.revision_id
            assert revision.status == "active"
            assert revision.parser_version == "text-test-v1"
            assert job.state == "succeeded"
            assert job.progress == 100


async def test_worker_retries_transient_embedding_failure_then_succeeds(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        storage = LocalVersionedObjectStorage(tmp_path)
        receipt = await _upload(env, storage, "worker-retry")
        await _generation(env)
        embedding = _FailOnceEmbeddingClient()
        worker = DocumentIngestionWorker(
            env.session_factory,
            storage,
            _Parser(),
            embedding,
            worker_id="worker-retry",
            lease_seconds=30,
            heartbeat_interval_seconds=1,
            chunk_size=100,
            chunk_overlap=0,
            max_chunks=20,
        )

        assert await worker.run_once() == receipt.job_id
        async with env.session_factory.begin() as session:
            job = await session.get(IngestionJobModel, receipt.job_id)
            assert job.state == "retry_wait"
            assert job.last_error_code == "embedding_provider_error"
            await session.execute(
                update(IngestionJobModel)
                .where(IngestionJobModel.id == receipt.job_id)
                .values(next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=60))
            )

        assert await worker.run_once() == receipt.job_id
        async with env.session_factory() as session:
            job = await session.get(IngestionJobModel, receipt.job_id)
            assert job.state == "succeeded"
            assert job.attempt_count == 2


async def test_worker_quarantines_checksum_mismatch_without_activation(tmp_path) -> None:
    class CorruptingStorage(LocalVersionedObjectStorage):
        async def read(self, storage_key: str) -> bytes:
            await super().read(storage_key)
            return b"x" * len(b"alpha beta gamma")

    async with TestEnvironment(storage_dir=tmp_path) as env:
        storage = CorruptingStorage(tmp_path)
        receipt = await _upload(env, storage, "worker-corrupt")
        await _generation(env)
        worker = DocumentIngestionWorker(
            env.session_factory,
            storage,
            _Parser(),
            _EmbeddingClient(),
            worker_id="worker-corrupt",
            lease_seconds=30,
            heartbeat_interval_seconds=1,
            chunk_size=100,
            chunk_overlap=0,
            max_chunks=20,
        )

        assert await worker.run_once() == receipt.job_id

        async with env.session_factory() as session:
            document = await session.get(DocumentModel, receipt.document_id)
            revision = await session.get(DocumentRevisionModel, receipt.revision_id)
            job = await session.get(IngestionJobModel, receipt.job_id)
            assert document.current_revision_id is None
            assert revision.status == "quarantined"
            assert revision.failure_code == "storage_checksum_mismatch"
            assert job.state == "failed"
            assert job.last_error_code == "storage_checksum_mismatch"


async def test_new_revision_stays_hidden_until_atomic_activation(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        storage = LocalVersionedObjectStorage(tmp_path)
        first = await _upload(env, storage, "revision-first")
        await _generation(env)
        worker = DocumentIngestionWorker(
            env.session_factory,
            storage,
            _Parser(),
            _EmbeddingClient(),
            worker_id="revision-worker",
            lease_seconds=30,
            heartbeat_interval_seconds=1,
            chunk_size=100,
            chunk_overlap=0,
            max_chunks=20,
        )
        await worker.run_once()

        async with env.session_factory() as session:
            document_revision = await session.scalar(
                select(DocumentModel.revision).where(DocumentModel.id == first.document_id)
            )

        second = await DocumentUploadService(
            env.session_factory,
            storage,
            max_upload_bytes=1024,
        ).upload_revision(
            principal=_principal(),
            document_id=first.document_id,
            expected_revision=document_revision,
            original_filename="worker-v2.txt",
            mime_type="text/plain",
            chunks=_chunks(b"delta epsilon zeta"),
            idempotency_key="revision-second",
        )

        async with env.session_factory() as session:
            document = await session.get(DocumentModel, first.document_id)
            first_revision = await session.get(DocumentRevisionModel, first.revision_id)
            second_revision = await session.get(DocumentRevisionModel, second.revision_id)
            active_sources = tuple(
                await session.scalars(
                    select(RetrievalUnitModel.document_revision_chunk_id)
                    .where(RetrievalUnitModel.active.is_(True))
                )
            )
            assert document.current_revision_id == first.revision_id
            assert first_revision.status == "active"
            assert second_revision.status == "pending"
            assert len(active_sources) == 1

        await worker.run_once()

        async with env.session_factory() as session:
            document = await session.get(DocumentModel, first.document_id)
            first_revision = await session.get(DocumentRevisionModel, first.revision_id)
            second_revision = await session.get(DocumentRevisionModel, second.revision_id)
            active_count = await session.scalar(
                select(func.count())
                .select_from(RetrievalUnitModel)
                .where(RetrievalUnitModel.active.is_(True))
            )
            active_content = await session.scalar(
                select(RetrievalUnitModel.content).where(RetrievalUnitModel.active.is_(True))
            )
            assert document.current_revision_id == second.revision_id
            assert first_revision.status == "ready"
            assert second_revision.status == "active"
            assert active_count == 1
            assert active_content == "delta epsilon zeta"

        with pytest.raises(ConcurrencyConflictException):
            await DocumentUploadService(
                env.session_factory,
                storage,
                max_upload_bytes=1024,
            ).upload_revision(
                principal=_principal(),
                document_id=first.document_id,
                expected_revision=document_revision,
                original_filename="stale.txt",
                mime_type="text/plain",
                chunks=_chunks(b"stale payload"),
                idempotency_key="revision-stale",
            )

        assert not tuple(path for path in (tmp_path / "staging").rglob("*") if path.is_file())


async def test_worker_round_trip_on_real_postgres(tmp_path) -> None:
    async with isolated_postgres_database() as (_, factory):
        principal = _principal()
        member_id = UUID(principal.subject_id)
        space_id = f"worker-pg-{uuid4().hex[:8]}"
        username = f"worker-pg-{uuid4().hex[:8]}"
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username=username,
                    username_normalized=username,
                    display_name="Worker PG",
                    status="active",
                    system_role="member",
                    force_password_change=False,
                )
            )
            session.add(Workspace(id=space_id, name="Worker PostgreSQL"))
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    space_id=space_id,
                    member_id=member_id,
                    role="owner",
                )
            )
        storage = LocalVersionedObjectStorage(tmp_path)
        receipt = await DocumentUploadService(
            factory,
            storage,
            max_upload_bytes=1024,
        ).upload_new(
            principal=principal,
            space_id=space_id,
            display_name="PostgreSQL worker",
            original_filename="postgres.txt",
            mime_type="text/plain",
            chunks=_chunks(b"postgres worker payload"),
            idempotency_key="worker-postgres",
        )
        async with factory.begin() as session:
            session.add(
                EmbeddingGenerationModel(
                    purpose="retrieval",
                    model_id="test-embedding",
                    dimensions=EMBED_DIM,
                    status="active",
                )
            )
        worker = DocumentIngestionWorker(
            factory,
            storage,
            _Parser(),
            _EmbeddingClient(),
            worker_id="worker-postgres",
            lease_seconds=30,
            heartbeat_interval_seconds=1,
            chunk_size=100,
            chunk_overlap=0,
            max_chunks=20,
        )

        assert await worker.run_once() == receipt.job_id

        async with factory() as session:
            document = await session.get(DocumentModel, receipt.document_id)
            job = await session.get(IngestionJobModel, receipt.job_id)
            assert document.current_revision_id == receipt.revision_id
            assert job.state == "succeeded"
