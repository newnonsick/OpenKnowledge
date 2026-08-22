import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select

from src.gateway.application.services.bounded_document_parser import ParsedDocument
from src.gateway.application.services.document_ingestion_worker import DocumentIngestionWorker
from src.gateway.application.services.document_upload_service import DocumentUploadService
from src.gateway.application.services.storage_consistency_service import StorageConsistencyService
from src.gateway.application.services.storage_manifest_service import StorageManifest, StorageManifestService
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionModel, EmbeddingGenerationModel, OperationalAlertModel, RetrievalUnitModel
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


class _Parser:
    async def parse(self, *, filename: str, mime_type: str, content: bytes) -> ParsedDocument:
        return ParsedDocument(text=content.decode(), parser_version="consistency-v1")


class _EmbeddingClient:
    dimension = 1024

    async def embed_texts(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]


async def _active_document(env, storage):
    receipt = await DocumentUploadService(
        env.session_factory,
        storage,
        max_upload_bytes=1024,
    ).upload_new(
        principal=_principal(),
        space_id="test_ws",
        display_name="Consistency",
        original_filename="consistent.txt",
        mime_type="text/plain",
        chunks=_chunks(b"consistent payload"),
        idempotency_key="consistency-upload",
    )
    async with env.session_factory.begin() as session:
        session.add(
            EmbeddingGenerationModel(
                purpose="retrieval",
                model_id="consistency-embedding",
                dimensions=1024,
                status="active",
            )
        )
    worker = DocumentIngestionWorker(
        env.session_factory,
        storage,
        _Parser(),
        _EmbeddingClient(),
        worker_id="consistency-worker",
        lease_seconds=30,
        heartbeat_interval_seconds=1,
        chunk_size=100,
        chunk_overlap=0,
        max_chunks=20,
    )
    await worker.run_once()
    return receipt


async def test_reconciler_quarantines_corrupt_referenced_object_and_deactivates_search(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        storage = LocalVersionedObjectStorage(tmp_path)
        receipt = await _active_document(env, storage)
        async with env.session_factory() as session:
            revision = await session.get(DocumentRevisionModel, receipt.revision_id)
            storage_path = tmp_path.joinpath(*revision.storage_key.split("/"))
        storage_path.write_bytes(b"x" * len(b"consistent payload"))

        result = await StorageConsistencyService(env.session_factory, storage).reconcile(
            now=datetime.now(timezone.utc),
            staging_ttl=timedelta(hours=1),
            orphan_grace=timedelta(hours=1),
        )

        assert result.quarantined_revision_ids == (receipt.revision_id,)
        assert result.alert_codes == ("storage_checksum_mismatch",)
        async with env.session_factory() as session:
            document = await session.get(DocumentModel, receipt.document_id)
            revision = await session.get(DocumentRevisionModel, receipt.revision_id)
            active = tuple(
                await session.scalars(
                    select(RetrievalUnitModel.id).where(RetrievalUnitModel.active.is_(True))
                )
            )
            alert = await session.scalar(
                select(OperationalAlertModel).where(
                    OperationalAlertModel.resource_id == str(receipt.revision_id)
                )
            )
            assert document.current_revision_id is None
            assert revision.status == "quarantined"
            assert revision.failure_code == "storage_checksum_mismatch"
            assert active == ()
            assert alert.code == "storage_checksum_mismatch"


async def test_reconciler_removes_only_expired_unreferenced_objects(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        storage = LocalVersionedObjectStorage(tmp_path)
        staged = await storage.stage(
            space_id="test_ws",
            upload_id=uuid4(),
            chunks=_chunks(b"expired staging"),
            max_bytes=100,
        )
        finalized_staging = await storage.stage(
            space_id="test_ws",
            upload_id=uuid4(),
            chunks=_chunks(b"expired orphan"),
            max_bytes=100,
        )
        orphan_key = await storage.finalize(
            finalized_staging.storage_key,
            space_id="test_ws",
            document_id=uuid4(),
            revision_id=uuid4(),
        )
        old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
        for key in (staged.storage_key, orphan_key):
            path = tmp_path.joinpath(*key.split("/"))
            os.utime(path, (old, old))

        result = await StorageConsistencyService(env.session_factory, storage).reconcile(
            now=datetime.now(timezone.utc),
            staging_ttl=timedelta(days=1),
            orphan_grace=timedelta(days=1),
        )

        assert set(result.deleted_storage_keys) == {staged.storage_key, orphan_key}
        assert not await storage.exists(staged.storage_key)
        assert not await storage.exists(orphan_key)


async def test_restore_manifest_detects_checksum_invalid_backup(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        storage = LocalVersionedObjectStorage(tmp_path)
        receipt = await _active_document(env, storage)
        service = StorageManifestService(env.session_factory, storage)

        manifest = await service.create()
        valid = await service.verify(manifest)
        assert valid.valid
        assert valid.checked_objects == 1

        async with env.session_factory() as session:
            key = await session.scalar(
                select(DocumentRevisionModel.storage_key).where(
                    DocumentRevisionModel.id == receipt.revision_id
                )
            )
        tmp_path.joinpath(*key.split("/")).write_bytes(b"x" * len(b"consistent payload"))

        invalid = await service.verify(manifest)
        assert not invalid.valid
        assert invalid.invalid_checksum_keys == (key,)

        omitted = StorageManifest(
            version=manifest.version,
            created_at=manifest.created_at,
            entries=(),
            digest_sha256=service._digest(manifest.version, manifest.created_at, ()),
        )
        missing_reference = await service.verify(omitted)
        assert not missing_reference.valid
        assert missing_reference.database_missing_manifest_keys == (key,)
