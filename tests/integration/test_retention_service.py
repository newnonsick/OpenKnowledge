from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from src.gateway.application.services.knowledge_management_service import KnowledgeManagementService
from src.gateway.application.services.retention_service import RetentionService
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, IdempotencyRecordModel, MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionChunkModel, DocumentRevisionModel, ProvenanceLinkModel
from src.gateway.infrastructure.persistence.models import KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from tests.integration.postgres_test_database import isolated_postgres_database


async def _bytes(value: bytes):
    yield value


class _CommitObservingStorage:
    def __init__(self, delegate, factory, revision_id) -> None:
        self._delegate = delegate
        self._factory = factory
        self._revision_id = revision_id

    async def delete(self, storage_key: str) -> bool:
        async with self._factory() as session:
            assert await session.get(DocumentRevisionModel, self._revision_id) is None
        return await self._delegate.delete(storage_key)


async def _grant_retention_execution(factory) -> None:
    async with factory.begin() as session:
        await session.execute(text("SET LOCAL ROLE gateway_maintenance"))
        assert await session.scalar(text("SELECT count(*) FROM knowledge_revisions")) == 0
        await session.execute(
            text(
                "SELECT * FROM gateway_run_retention("
                "now() - interval '365 days', now() - interval '1095 days', "
                "now() - interval '30 days', 1)"
            )
        )
        await session.execute(text("RESET ROLE"))
        await session.execute(
            text(
                "GRANT EXECUTE ON FUNCTION "
                "gateway_run_retention(timestamptz, timestamptz, timestamptz, integer) "
                "TO CURRENT_USER"
            )
        )


async def test_retention_purges_only_eligible_archives_and_deletes_objects_after_commit(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    recent = now - timedelta(days=10)
    member_id = uuid4()
    old_knowledge_id = uuid4()
    recent_knowledge_id = uuid4()
    old_document_id = uuid4()
    old_revision_id = uuid4()
    content = b"retention object"
    storage = LocalVersionedObjectStorage(tmp_path)
    staged = await storage.stage(
        space_id="retention",
        upload_id=uuid4(),
        chunks=_bytes(content),
        max_bytes=1024,
    )
    storage_key = await storage.finalize(
        staged.storage_key,
        space_id="retention",
        document_id=old_document_id,
        revision_id=old_revision_id,
    )

    async with isolated_postgres_database() as (_, factory):
        await _grant_retention_execution(factory)
        async with factory() as contract_session:
            assert await contract_session.scalar(
                text(
                    "SELECT has_table_privilege('gateway_maintenance', "
                    "'public.knowledge_revisions', 'SELECT')"
                )
            )
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="retention-owner",
                    username_normalized="retention-owner",
                    display_name="Retention Owner",
                    status="active",
                    system_role="member",
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="retention", name="Retention", created_by_member_id=member_id))
            await session.flush()
            session.add(SpaceMembershipModel(space_id="retention", member_id=member_id, role="owner"))
            for item_id, archived_at in ((old_knowledge_id, old), (recent_knowledge_id, recent)):
                revision_id = uuid4()
                item = KnowledgeItem(
                    id=item_id,
                    workspace_id="retention",
                    title="Archived",
                    content="Archived content",
                    current_revision_id=None,
                    tags=[],
                    is_global=False,
                    is_deleted=True,
                    archived_at=archived_at,
                    created_at=archived_at,
                    updated_at=archived_at,
                )
                session.add(item)
                await session.flush()
                session.add(
                    KnowledgeRevision(
                        id=revision_id,
                        item_id=item_id,
                        space_id="retention",
                        version=1,
                        title="Archived",
                        content_hash="a" * 64,
                        content="Archived content",
                        tags=[],
                        author="retention",
                        author_member_id=member_id,
                        created_at=archived_at,
                    )
                )
                await session.flush()
                item.current_revision_id = revision_id
            session.add(
                DocumentModel(
                    id=old_document_id,
                    space_id="retention",
                    display_name="Archived Document",
                    current_revision_id=None,
                    created_by_member_id=member_id,
                    archived_at=old,
                    created_at=old,
                    updated_at=old,
                )
            )
            await session.flush()
            session.add(
                DocumentRevisionModel(
                    id=old_revision_id,
                    document_id=old_document_id,
                    space_id="retention",
                    version=1,
                    original_filename="retention.txt",
                    mime_type="text/plain",
                    size_bytes=len(content),
                    checksum_sha256=hashlib.sha256(content).hexdigest(),
                    storage_key=storage_key,
                    status="ready",
                    created_by_member_id=member_id,
                    created_at=old,
                    ready_at=old,
                )
            )
            await session.flush()
            document = await session.get(DocumentModel, old_document_id)
            document.current_revision_id = old_revision_id
            session.add(
                IdempotencyRecordModel(
                    actor_id=str(member_id),
                    operation="retention-test",
                    idempotency_key="expired",
                    request_hash="b" * 64,
                    expires_at=old,
                )
            )

        result = await RetentionService(
            factory,
            _CommitObservingStorage(storage, factory, old_revision_id),
        ).purge(
            now=now,
            archive_retention=timedelta(days=365),
            revision_retention=timedelta(days=1095),
            operational_retention=timedelta(days=30),
            batch_size=10,
        )

        assert result is not None
        assert result.purged_counts["knowledge_item"] == 1
        assert result.purged_counts["document"] == 1
        assert result.purged_counts["idempotency_record"] == 1
        assert result.deleted_storage_keys == (storage_key,)
        assert result.failed_storage_keys == ()
        assert not await storage.exists(storage_key)
        async with factory() as session:
            assert await session.get(KnowledgeItem, old_knowledge_id) is None
            assert await session.get(KnowledgeItem, recent_knowledge_id) is not None
            assert await session.get(DocumentModel, old_document_id) is None
            assert await session.scalar(select(IdempotencyRecordModel.id)) is None
            audit = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.action == "maintenance.retention.purge"
                )
            )
            assert audit is not None
            assert audit.actor_kind == "system"
            assert audit.details["purged_records"] >= 3


async def test_retention_rejects_unbounded_or_nonpositive_policy(tmp_path) -> None:
    async with isolated_postgres_database() as (_, factory):
        await _grant_retention_execution(factory)
        service = RetentionService(factory, LocalVersionedObjectStorage(tmp_path))
        async with factory() as session:
            with pytest.raises(DBAPIError, match="Retention cutoffs violate the database safety floor"):
                await session.execute(
                    text("SELECT * FROM gateway_run_retention(now(), now(), now(), 1)")
                )
            await session.rollback()
            with pytest.raises(DBAPIError, match="Retention batch size must be between 1 and 1000"):
                await session.execute(
                    text(
                        "SELECT * FROM gateway_run_retention("
                        "now() - interval '365 days', now() - interval '1095 days', "
                        "now() - interval '30 days', NULL)"
                    )
                )
            await session.rollback()
        for batch_size in (0, 1001):
            try:
                await service.purge(
                    now=datetime.now(timezone.utc),
                    archive_retention=timedelta(days=1),
                    revision_retention=timedelta(days=1),
                    operational_retention=timedelta(days=1),
                    batch_size=batch_size,
                )
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid retention batch size was accepted")


async def test_retention_preserves_direct_and_chunk_provenance_without_blocking_other_categories(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    member_id = uuid4()
    knowledge_id = uuid4()
    knowledge_revision_id = uuid4()
    direct_document_id = uuid4()
    direct_revision_id = uuid4()
    chunk_document_id = uuid4()
    chunk_revision_id = uuid4()
    chunk_id = uuid4()
    async with isolated_postgres_database() as (_, factory):
        await _grant_retention_execution(factory)
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="provenance-owner",
                    username_normalized="provenance-owner",
                    display_name="Provenance Owner",
                    status="active",
                    system_role="member",
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="provenance-retention", name="Provenance", created_by_member_id=member_id))
            await session.flush()
            session.add(SpaceMembershipModel(space_id="provenance-retention", member_id=member_id, role="owner"))
            knowledge = KnowledgeItem(
                id=knowledge_id,
                workspace_id="provenance-retention",
                title="Retained knowledge",
                content="Retained",
                current_revision_id=None,
                tags=[],
                is_global=False,
                is_deleted=False,
            )
            session.add(knowledge)
            for document_id, display_name in (
                (direct_document_id, "Direct provenance"),
                (chunk_document_id, "Chunk provenance"),
            ):
                session.add(
                    DocumentModel(
                        id=document_id,
                        space_id="provenance-retention",
                        display_name=display_name,
                        current_revision_id=None,
                        created_by_member_id=member_id,
                        created_at=old,
                        updated_at=old,
                    )
                )
            await session.flush()
            session.add(
                KnowledgeRevision(
                    id=knowledge_revision_id,
                    item_id=knowledge_id,
                    space_id="provenance-retention",
                    version=1,
                    title="Retained knowledge",
                    content_hash="c" * 64,
                    content="Retained",
                    tags=[],
                    author="retention",
                    author_member_id=member_id,
                )
            )
            session.add_all(
                [
                    DocumentRevisionModel(
                        id=direct_revision_id,
                        document_id=direct_document_id,
                        space_id="provenance-retention",
                        version=1,
                        original_filename="direct.txt",
                        mime_type="text/plain",
                        size_bytes=0,
                        checksum_sha256=hashlib.sha256(b"").hexdigest(),
                        status="ready",
                        created_by_member_id=member_id,
                        created_at=old,
                        ready_at=old,
                    ),
                    DocumentRevisionModel(
                        id=chunk_revision_id,
                        document_id=chunk_document_id,
                        space_id="provenance-retention",
                        version=1,
                        original_filename="chunk.txt",
                        mime_type="text/plain",
                        size_bytes=0,
                        checksum_sha256=hashlib.sha256(b"").hexdigest(),
                        status="ready",
                        created_by_member_id=member_id,
                        created_at=old,
                        ready_at=old,
                    ),
                ]
            )
            await session.flush()
            knowledge.current_revision_id = knowledge_revision_id
            session.add(
                DocumentRevisionChunkModel(
                    id=chunk_id,
                    document_revision_id=chunk_revision_id,
                    document_id=chunk_document_id,
                    space_id="provenance-retention",
                    chunk_index=0,
                    content="Retained source",
                    content_hash="d" * 64,
                    language="en",
                )
            )
            await session.flush()
            session.add_all(
                [
                    ProvenanceLinkModel(
                        space_id="provenance-retention",
                        knowledge_revision_id=knowledge_revision_id,
                        source_type="document_revision",
                        document_revision_id=direct_revision_id,
                        actor_member_id=member_id,
                    ),
                    ProvenanceLinkModel(
                        space_id="provenance-retention",
                        knowledge_revision_id=knowledge_revision_id,
                        source_type="document_chunk",
                        document_revision_chunk_id=chunk_id,
                        actor_member_id=member_id,
                    ),
                    IdempotencyRecordModel(
                        actor_id=str(member_id),
                        operation="provenance-retention",
                        idempotency_key="expired",
                        request_hash="e" * 64,
                        expires_at=old,
                    ),
                ]
            )

        archive_session = factory()
        insert_session = factory()
        insert_task = None
        try:
            async with archive_session.begin():
                document = await archive_session.scalar(
                    select(DocumentModel)
                    .where(DocumentModel.id == direct_document_id)
                    .with_for_update()
                )
                document.archived_at = old
                await archive_session.flush()
                insert_task = asyncio.create_task(
                    insert_session.execute(
                        text(
                            "INSERT INTO provenance_links ("
                            "id, space_id, knowledge_revision_id, source_type, "
                            "document_revision_id, actor_member_id"
                            ") VALUES ("
                            ":id, 'provenance-retention', :knowledge_revision_id, "
                            "'document_revision', :document_revision_id, :actor_member_id"
                            ")"
                        ),
                        {
                            "id": uuid4(),
                            "knowledge_revision_id": knowledge_revision_id,
                            "document_revision_id": direct_revision_id,
                            "actor_member_id": member_id,
                        },
                    )
                )
                await asyncio.sleep(0.1)
                assert not insert_task.done()
            with pytest.raises(DBAPIError, match="Archived documents cannot receive new provenance links"):
                await insert_task
            await insert_session.rollback()
        finally:
            if insert_task is not None and not insert_task.done():
                insert_task.cancel()
            await insert_session.close()
            await archive_session.close()

        async with factory.begin() as session:
            document = await session.get(DocumentModel, chunk_document_id)
            document.archived_at = old

        result = await RetentionService(
            factory,
            LocalVersionedObjectStorage(tmp_path),
        ).purge(
            now=now,
            archive_retention=timedelta(days=365),
            revision_retention=timedelta(days=1095),
            operational_retention=timedelta(days=30),
            batch_size=100,
        )

        assert result is not None
        assert result.purged_counts == {"idempotency_record": 1}
        async with factory() as session:
            assert await session.get(DocumentModel, direct_document_id) is not None
            assert await session.get(DocumentModel, chunk_document_id) is not None
            assert await session.get(DocumentRevisionModel, direct_revision_id) is not None
            assert await session.get(DocumentRevisionModel, chunk_revision_id) is not None


async def test_retention_purges_knowledge_archived_through_the_management_service(tmp_path) -> None:
    old = datetime.now(timezone.utc) - timedelta(days=400)
    member_id = uuid4()
    principal = Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read", "knowledge:write"}),
    )
    async with isolated_postgres_database() as (_, factory):
        await _grant_retention_execution(factory)
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="retention-real-archive",
                    username_normalized="retention-real-archive",
                    display_name="Retention Real Archive",
                    status="active",
                    system_role="member",
                    force_password_change=False,
                )
            )
            session.add(
                Workspace(
                    id="retention-real-archive",
                    name="Retention Real Archive",
                    created_by_member_id=member_id,
                )
            )
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    space_id="retention-real-archive",
                    member_id=member_id,
                    role="owner",
                )
            )
            service = KnowledgeManagementService(session)
            created = await service.create(
                principal,
                space_id="retention-real-archive",
                title="Archived through service",
                content="Production archive path",
                tags=[],
                request_id="retention-create",
            )
            await service.delete(
                principal,
                created.id,
                expected_version=1,
                request_id="retention-delete",
            )
            archived = await session.get(KnowledgeItem, created.id)
            assert archived.current_revision_id is not None
            archived.archived_at = old

        result = await RetentionService(
            factory,
            LocalVersionedObjectStorage(tmp_path),
        ).purge(
            now=datetime.now(timezone.utc),
            archive_retention=timedelta(days=365),
            revision_retention=timedelta(days=1095),
            operational_retention=timedelta(days=30),
            batch_size=10,
        )
        assert result is not None
        assert result.purged_counts["knowledge_item"] == 1
        async with factory() as session:
            assert await session.get(KnowledgeItem, created.id) is None


async def test_retention_eventually_purges_a_document_larger_than_one_batch_and_serializes_workers(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    member_id = uuid4()
    document_id = uuid4()
    revision_id = uuid4()
    async with isolated_postgres_database() as (_, factory):
        await _grant_retention_execution(factory)
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="retention-large-document",
                    username_normalized="retention-large-document",
                    display_name="Retention Large Document",
                    status="active",
                    system_role="member",
                    force_password_change=False,
                )
            )
            session.add(
                Workspace(
                    id="retention-large-document",
                    name="Retention Large Document",
                    created_by_member_id=member_id,
                )
            )
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    space_id="retention-large-document",
                    member_id=member_id,
                    role="owner",
                )
            )
            document = DocumentModel(
                id=document_id,
                space_id="retention-large-document",
                display_name="Large archived document",
                current_revision_id=None,
                created_by_member_id=member_id,
                archived_at=old,
                created_at=old,
                updated_at=old,
            )
            session.add(document)
            await session.flush()
            session.add(
                DocumentRevisionModel(
                    id=revision_id,
                    document_id=document_id,
                    space_id="retention-large-document",
                    version=1,
                    original_filename="large.txt",
                    mime_type="text/plain",
                    size_bytes=25,
                    checksum_sha256="f" * 64,
                    status="ready",
                    created_by_member_id=member_id,
                    created_at=old,
                    ready_at=old,
                )
            )
            await session.flush()
            document.current_revision_id = revision_id
            session.add_all(
                [
                    DocumentRevisionChunkModel(
                        document_revision_id=revision_id,
                        document_id=document_id,
                        space_id="retention-large-document",
                        chunk_index=index,
                        content=f"chunk {index}",
                        content_hash=f"{index:064x}",
                    )
                    for index in range(25)
                ]
            )

        service = RetentionService(factory, LocalVersionedObjectStorage(tmp_path))
        async with factory.begin() as lock_session:
            await lock_session.execute(
                text("SELECT pg_advisory_xact_lock(739814621451062318)")
            )
            assert await service.purge(
                now=now,
                archive_retention=timedelta(days=365),
                revision_retention=timedelta(days=1095),
                operational_retention=timedelta(days=30),
                batch_size=10,
            ) is None

        chunk_counts = []
        for _ in range(5):
            result = await service.purge(
                now=now,
                archive_retention=timedelta(days=365),
                revision_retention=timedelta(days=1095),
                operational_retention=timedelta(days=30),
                batch_size=10,
            )
            assert result is not None
            chunk_counts.append(result.purged_counts.get("document_revision_chunk", 0))
            async with factory() as session:
                if await session.get(DocumentModel, document_id) is None:
                    break
        assert chunk_counts == [10, 10, 5]
        async with factory() as session:
            assert await session.get(DocumentModel, document_id) is None
