import asyncio
from uuid import UUID
from uuid import uuid4

from sqlalchemy import func, select
import pytest

from src.gateway.application.services.document_upload_service import DocumentUploadService
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.domain.exceptions import ConcurrencyConflictException, StorageException
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionModel, IngestionJobModel, JobOutboxModel
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from tests.e2e.harness.test_env import TestEnvironment
from tests.integration.postgres_test_database import isolated_postgres_database


async def _chunks(value: bytes):
    midpoint = max(len(value) // 2, 1)
    yield value[:midpoint]
    yield value[midpoint:]


def _principal() -> Principal:
    import hashlib

    digest = hashlib.sha256(b"sk-test-admin").digest()
    return Principal(
        subject_id=str(UUID(bytes=digest[:16])),
        kind=PrincipalKind.COMPATIBILITY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:write"}),
    )


async def test_upload_creates_pending_revision_job_and_finalized_immutable_object(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        storage = LocalVersionedObjectStorage(tmp_path)
        service = DocumentUploadService(env.session_factory, storage, max_upload_bytes=1024)
        principal = _principal()

        receipt = await service.upload_new(
            principal=principal,
            space_id="test_ws",
            display_name="Family Notes",
            original_filename="notes.txt",
            mime_type="text/plain",
            chunks=_chunks(b"family payload"),
            idempotency_key="upload-1",
        )

        async with env.session_factory() as session:
            document = await session.get(DocumentModel, receipt.document_id)
            revision = await session.get(DocumentRevisionModel, receipt.revision_id)
            job = await session.get(IngestionJobModel, receipt.job_id)
            assert document.current_revision_id is None
            assert revision.status == "pending"
            assert revision.staging_storage_key is None
            assert revision.storage_key is not None
            assert job.state == "queued"
            assert await storage.read(revision.storage_key) == b"family payload"

        with pytest.raises(ConcurrencyConflictException):
            await service.upload_new(
                principal=principal,
                space_id="test_ws",
                display_name="Ignored Retry",
                original_filename="retry.txt",
                mime_type="text/plain",
                chunks=_chunks(b"different bytes"),
                idempotency_key="upload-1",
            )
        second = await service.upload_new(
            principal=principal,
            space_id="test_ws",
            display_name="Family Notes",
            original_filename="notes.txt",
            mime_type="text/plain",
            chunks=_chunks(b"family payload"),
            idempotency_key="upload-1",
        )
        assert second.document_id == receipt.document_id
        assert second.revision_id == receipt.revision_id
        assert second.job_id == receipt.job_id

        async with env.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(DocumentModel)) == 1
            assert await session.scalar(select(func.count()).select_from(IngestionJobModel)) == 1


async def test_same_space_checksum_is_reported_without_silent_merge(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        service = DocumentUploadService(
            env.session_factory,
            LocalVersionedObjectStorage(tmp_path),
            max_upload_bytes=1024,
        )
        principal = _principal()
        first = await service.upload_new(
            principal=principal,
            space_id="test_ws",
            display_name="First",
            original_filename="first.txt",
            mime_type="text/plain",
            chunks=_chunks(b"same"),
            idempotency_key="first",
        )
        second = await service.upload_new(
            principal=principal,
            space_id="test_ws",
            display_name="Second",
            original_filename="second.txt",
            mime_type="text/plain",
            chunks=_chunks(b"same"),
            idempotency_key="second",
        )

        assert second.document_id != first.document_id
        assert second.duplicate_candidate_revision_id == first.revision_id


async def test_finalize_failure_never_queues_partial_revision_and_retry_resumes(tmp_path) -> None:
    class FailOnceStorage(LocalVersionedObjectStorage):
        fail = True

        async def finalize(self, *args, **kwargs):
            if self.fail:
                raise StorageException("Injected finalize failure.")
            return await super().finalize(*args, **kwargs)

    async with TestEnvironment(storage_dir=tmp_path) as env:
        storage = FailOnceStorage(tmp_path)
        service = DocumentUploadService(env.session_factory, storage, max_upload_bytes=1024)
        principal = _principal()
        with pytest.raises(StorageException):
            await service.upload_new(
                principal=principal,
                space_id="test_ws",
                display_name="Recoverable",
                original_filename="recover.txt",
                mime_type="text/plain",
                chunks=_chunks(b"recover"),
                idempotency_key="recover-finalize",
            )

        async with env.session_factory() as session:
            job = await session.scalar(
                select(IngestionJobModel).where(
                    IngestionJobModel.idempotency_key == "recover-finalize"
                )
            )
            revision = await session.get(DocumentRevisionModel, job.document_revision_id)
            document = await session.get(DocumentModel, job.document_id)
            assert job.state == "preparing"
            assert revision.staging_storage_key is not None
            assert revision.storage_key is None
            assert document.current_revision_id is None

        storage.fail = False
        receipt = await service.upload_new(
            principal=principal,
            space_id="test_ws",
            display_name="Recoverable",
            original_filename="recover.txt",
            mime_type="text/plain",
            chunks=_chunks(b"recover"),
            idempotency_key="recover-finalize",
        )
        assert receipt.job_state == "queued"


async def test_concurrent_identical_upload_retry_converges_on_postgres(tmp_path) -> None:
    async with isolated_postgres_database() as (_, factory):
        member_id = uuid4()
        space_id = f"upload-race-{uuid4().hex[:8]}"
        username = f"upload-race-{uuid4().hex[:8]}"
        principal = Principal(
            subject_id=str(member_id),
            kind=PrincipalKind.SESSION,
            system_role=SystemRole.MEMBER,
            scopes=frozenset({"knowledge:write"}),
        )
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username=username,
                    username_normalized=username,
                    display_name="Upload Race",
                    status="active",
                    system_role="member",
                    force_password_change=False,
                )
            )
            session.add(Workspace(id=space_id, name="Upload Race"))
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    space_id=space_id,
                    member_id=member_id,
                    role="owner",
                )
            )
        storage = LocalVersionedObjectStorage(tmp_path)

        async def upload():
            return await DocumentUploadService(
                factory,
                storage,
                max_upload_bytes=1024,
            ).upload_new(
                principal=principal,
                space_id=space_id,
                display_name="Concurrent",
                original_filename="concurrent.txt",
                mime_type="text/plain",
                chunks=_chunks(b"concurrent body"),
                idempotency_key="concurrent-upload",
            )

        first, second = await asyncio.gather(upload(), upload())

        assert first.document_id == second.document_id
        assert first.revision_id == second.revision_id
        assert first.job_id == second.job_id
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(DocumentModel)) == 1
            assert await session.scalar(select(func.count()).select_from(IngestionJobModel)) == 1
            assert await session.scalar(select(func.count()).select_from(JobOutboxModel)) == 1
        assert not tuple(path for path in (tmp_path / "staging").rglob("*") if path.is_file())
