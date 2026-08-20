from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select

from src.gateway.application.services.legacy_backfill_service import LegacyBackfillService
from src.gateway.application.services.storage_manifest_service import StorageManifestService
from src.gateway.infrastructure.persistence.identity_models import MemberModel
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionModel, EmbeddingGenerationModel, MigrationBackfillRunModel, ProvenanceLinkModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import DocumentChunk, DocumentFile, KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from tests.e2e.harness.test_env import TestEnvironment
from tests.integration.postgres_test_database import isolated_postgres_database


def _actor_id() -> UUID:
    import hashlib

    return UUID(bytes=hashlib.sha256(b"sk-test-admin").digest()[:16])


async def _seed_legacy_document(env, storage, *, created_at, suffix: str):
    document_id = uuid4()
    chunk_id = uuid4()
    filename = f"legacy-{suffix}.txt"
    content = f"legacy content {suffix}".encode()
    path = await storage.save_file("test_ws", document_id, filename, content)
    async with env.session_factory.begin() as session:
        session.add(
            DocumentFile(
                id=document_id,
                workspace_id="test_ws",
                filename=filename,
                file_path=str(path),
                file_size=len(content),
                mime_type="text/plain",
                created_at=created_at,
            )
        )
        await session.flush()
        session.add(
            DocumentChunk(
                id=chunk_id,
                document_id=document_id,
                workspace_id="test_ws",
                chunk_index=0,
                content=content.decode(),
                embedding=[1.0] + [0.0] * 1023,
                metadata_={"legacy": True},
            )
        )
    return document_id, chunk_id


async def test_document_backfill_uses_recorded_high_water_then_bounded_delta(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        legacy_storage = LocalStorageAdapter(tmp_path / "legacy")
        versioned_storage = LocalVersionedObjectStorage(tmp_path / "versioned")
        started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        first_id, first_chunk_id = await _seed_legacy_document(
            env,
            legacy_storage,
            created_at=started_at,
            suffix="first",
        )
        async with env.session_factory.begin() as session:
            session.add(
                EmbeddingGenerationModel(
                    purpose="retrieval",
                    model_id="legacy-embedding",
                    dimensions=1024,
                    status="active",
                )
            )
        service = LegacyBackfillService(
            env.session_factory,
            legacy_storage,
            versioned_storage,
            migration_actor_id=_actor_id(),
        )
        snapshot = await service.start_document_snapshot()
        assert snapshot.high_water_id == first_id

        second_id, second_chunk_id = await _seed_legacy_document(
            env,
            legacy_storage,
            created_at=started_at + timedelta(minutes=1),
            suffix="second",
        )

        assert await service.run_document_snapshot_batch(batch_size=1) == 1
        assert await service.run_document_snapshot_batch(batch_size=1) == 0
        assert await service.run_document_delta_batch(batch_size=1) == 1
        assert await service.run_document_delta_batch(batch_size=1) == 0
        assert await service.run_document_delta_batch(batch_size=1) == 0

        async with env.session_factory() as session:
            run = await session.get(MigrationBackfillRunModel, "legacy_documents")
            documents = tuple(await session.scalars(select(DocumentModel).order_by(DocumentModel.id)))
            revisions = tuple(await session.scalars(select(DocumentRevisionModel).order_by(DocumentRevisionModel.document_id)))
            active_count = await session.scalar(
                select(func.count()).select_from(RetrievalUnitModel).where(
                    RetrievalUnitModel.active.is_(True)
                )
            )
            retrieval_chunks = set(
                await session.scalars(select(RetrievalUnitModel.document_revision_chunk_id))
            )
            assert run.phase == "complete"
            assert run.rows_migrated == 2
            assert {document.id for document in documents} == {first_id, second_id}
            assert all(revision.status == "active" for revision in revisions)
            assert active_count == 2
            assert retrieval_chunks == {first_chunk_id, second_chunk_id}

        manifest = await StorageManifestService(
            env.session_factory,
            versioned_storage,
        ).create()
        assert (await StorageManifestService(
            env.session_factory,
            versioned_storage,
        ).verify(manifest)).valid
        assert len(manifest.entries) == 2


async def test_knowledge_provenance_backfill_is_idempotent_and_attributed(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        item_id = uuid4()
        revision_id = uuid4()
        async with env.session_factory.begin() as session:
            item = KnowledgeItem(
                id=item_id,
                workspace_id="test_ws",
                title="Legacy knowledge",
                content="legacy",
            )
            session.add(item)
            await session.flush()
            revision = KnowledgeRevision(
                id=revision_id,
                item_id=item_id,
                space_id="test_ws",
                version=1,
                title="Legacy knowledge",
                content="legacy",
                content_hash="a" * 64,
                author="legacy",
            )
            session.add(revision)
            await session.flush()
            item.current_revision_id = revision_id

        service = LegacyBackfillService(
            env.session_factory,
            LocalStorageAdapter(tmp_path / "legacy"),
            LocalVersionedObjectStorage(tmp_path / "versioned"),
            migration_actor_id=_actor_id(),
        )

        assert await service.run_knowledge_provenance_batch(batch_size=10) == 1
        assert await service.run_knowledge_provenance_batch(batch_size=10) == 0

        async with env.session_factory() as session:
            link = await session.scalar(
                select(ProvenanceLinkModel).where(
                    ProvenanceLinkModel.knowledge_revision_id == revision_id
                )
            )
            assert link.actor_member_id == _actor_id()
            assert link.source_type == "manual"
            assert link.source_metadata == {"migration": "legacy_knowledge"}


async def test_document_backfill_round_trip_on_postgres_pgvector(tmp_path) -> None:
    async with isolated_postgres_database() as (_, factory):
        actor_id = uuid4()
        space_id = f"backfill-{uuid4().hex[:8]}"
        legacy_storage = LocalStorageAdapter(tmp_path / "legacy-pg")
        versioned_storage = LocalVersionedObjectStorage(tmp_path / "versioned-pg")
        document_id = uuid4()
        chunk_id = uuid4()
        filename = "postgres-backfill.txt"
        content = b"postgres backfill content"
        path = await legacy_storage.save_file(space_id, document_id, filename, content)
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=actor_id,
                    username=f"migration-{uuid4().hex[:8]}",
                    username_normalized=f"migration-{uuid4().hex[:8]}",
                    display_name="Migration Actor",
                    status="disabled",
                    system_role="member",
                    force_password_change=False,
                )
            )
            session.add(Workspace(id=space_id, name="Backfill PostgreSQL"))
            await session.flush()
            session.add(
                DocumentFile(
                    id=document_id,
                    workspace_id=space_id,
                    filename=filename,
                    file_path=str(path),
                    file_size=len(content),
                    mime_type="text/plain",
                )
            )
            session.add(
                EmbeddingGenerationModel(
                    purpose="retrieval",
                    model_id="legacy-postgres",
                    dimensions=1024,
                    status="active",
                )
            )
            await session.flush()
            session.add(
                DocumentChunk(
                    id=chunk_id,
                    document_id=document_id,
                    workspace_id=space_id,
                    chunk_index=0,
                    content=content.decode(),
                    embedding=[1.0] + [0.0] * 1023,
                    metadata_={},
                )
            )

        service = LegacyBackfillService(
            factory,
            legacy_storage,
            versioned_storage,
            migration_actor_id=actor_id,
        )
        assert await service.run_document_snapshot_batch(batch_size=10) == 1
        assert await service.run_document_snapshot_batch(batch_size=10) == 0
        assert await service.run_document_delta_batch(batch_size=10) == 0

        async with factory() as session:
            document = await session.get(DocumentModel, document_id)
            revision = await session.get(DocumentRevisionModel, document.current_revision_id)
            unit = await session.scalar(
                select(RetrievalUnitModel).where(
                    RetrievalUnitModel.document_revision_chunk_id == chunk_id
                )
            )
            assert revision.status == "active"
            assert unit.active
            assert len(unit.embedding) == 1024
