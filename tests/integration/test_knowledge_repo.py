"""Integration tests for KnowledgeRepository and DocumentRepository with database persistence."""

from __future__ import annotations

import hashlib
from uuid import uuid4
import pytest

from src.gateway.domain.entities import (
    DocumentChunk,
    DocumentFile,
    KnowledgeItem,
    KnowledgeRevision,
)
from src.gateway.domain.exceptions import ConcurrencyConflictException
from src.gateway.infrastructure.persistence.document_repository import DocumentRepository
from src.gateway.infrastructure.persistence.knowledge_repository import KnowledgeRepository
from src.gateway.infrastructure.persistence.models import EMBED_DIM
from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.integration
@pytest.mark.asyncio
async def test_knowledge_repository_lifecycle():
    """Verify KnowledgeRepository creation, OCC update, history, and soft deletion."""
    async with TestEnvironment() as env:
        repo = KnowledgeRepository(session_factory=env.session_factory)

        item_id = uuid4()
        initial_content = "PostgreSQL pgvector storage guidelines."
        initial_hash = hashlib.sha256(initial_content.encode("utf-8")).hexdigest()

        rev_v1 = KnowledgeRevision(
            id=uuid4(),
            item_id=item_id,
            version=1,
            title="DB Guidelines",
            content=initial_content,
            content_hash=initial_hash,
            embedding=[0.1] * EMBED_DIM,
            author="lead_dev",
        )

        item = KnowledgeItem(
            id=item_id,
            workspace_id="global",
            is_global=True,
            version=1,
            title="DB Guidelines",
            content=initial_content,
            current_revision=rev_v1,
        )

        # 1. Create item
        saved_item = await repo.create_item(item, rev_v1)
        assert saved_item.id == item_id
        assert saved_item.version == 1
        assert saved_item.current_revision is not None
        assert saved_item.current_revision.embedding == pytest.approx([0.1] * EMBED_DIM, abs=1e-3)

        # 2. Get item
        fetched = await repo.get_item_by_id(item_id)
        assert fetched is not None
        assert fetched.version == 1
        assert fetched.content == initial_content
        assert fetched.current_revision is not None
        assert fetched.current_revision.embedding == pytest.approx([0.1] * EMBED_DIM, abs=1e-3)

        # 3. Update with valid OCC expected_version=1 -> v2
        updated_content = "PostgreSQL pgvector storage guidelines v2 with HNSW indexing."
        rev_v2 = KnowledgeRevision(
            id=uuid4(),
            item_id=item_id,
            version=2,
            title="DB Guidelines",
            content=updated_content,
            content_hash=hashlib.sha256(updated_content.encode("utf-8")).hexdigest(),
            embedding=[0.2] * EMBED_DIM,
            author="lead_dev",
        )
        updated_item = await repo.update_item_occ(
            item_id=item_id,
            expected_version=1,
            new_revision=rev_v2,
        )
        assert updated_item.version == 2
        assert updated_item.content == updated_content
        assert updated_item.current_revision is not None
        assert updated_item.current_revision.embedding == pytest.approx([0.2] * EMBED_DIM, abs=1e-3)

        # 4. Attempt update with stale expected_version=1 -> raises 409
        rev_v3_stale = KnowledgeRevision(
            id=uuid4(),
            item_id=item_id,
            version=2,
            content="Stale write",
            content_hash="abc",
        )
        with pytest.raises(ConcurrencyConflictException):
            await repo.update_item_occ(
                item_id=item_id,
                expected_version=1,
                new_revision=rev_v3_stale,
            )

        # 5. List revisions
        history = await repo.list_revisions(item_id)
        assert len(history) == 2
        assert history[0].version == 1
        assert history[0].embedding == pytest.approx([0.1] * EMBED_DIM, abs=1e-3)
        assert history[1].version == 2
        assert history[1].embedding == pytest.approx([0.2] * EMBED_DIM, abs=1e-3)

        # 6. Fetch historical version 1
        v1_snapshot = await repo.get_item_by_id(item_id, version=1)
        assert v1_snapshot is not None
        assert v1_snapshot.version == 1
        assert v1_snapshot.content == initial_content
        assert v1_snapshot.current_revision is not None
        assert v1_snapshot.current_revision.embedding == pytest.approx([0.1] * EMBED_DIM, abs=1e-3)

        # 7. Soft delete
        del_result = await repo.soft_delete_item(item_id, expected_version=2)
        assert del_result is True

        # 8. Get active item returns None
        assert await repo.get_item_by_id(item_id) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_knowledge_repository_vector_search():
    """Verify KnowledgeRepository vector search ranks items by cosine similarity correctly."""
    async with TestEnvironment() as env:
        repo = KnowledgeRepository(session_factory=env.session_factory)

        # Item 1: High similarity with query vector [1.0, 0.0, 0.0]
        id1 = uuid4()
        rev1 = KnowledgeRevision(
            id=uuid4(),
            item_id=id1,
            version=1,
            title="Clean Architecture Guide",
            content="Clean Architecture in Python with layered domains.",
            content_hash=hashlib.sha256(b"Clean Architecture in Python").hexdigest(),
            embedding=[0.95, 0.05, 0.0] + [0.0] * (EMBED_DIM - 3),
        )
        item1 = KnowledgeItem(
            id=id1,
            workspace_id="test_ws",
            title="Clean Architecture Guide",
            content="Clean Architecture in Python with layered domains.",
            current_revision=rev1,
        )
        await repo.create_item(item1, rev1)

        # Item 2: Low similarity with query vector [1.0, 0.0, 0.0]
        id2 = uuid4()
        rev2 = KnowledgeRevision(
            id=uuid4(),
            item_id=id2,
            version=1,
            title="Cooking Recipes",
            content="Italian pasta and pizza dough recipes.",
            content_hash=hashlib.sha256(b"Italian pasta").hexdigest(),
            embedding=[0.0, 0.95, 0.05] + [0.0] * (EMBED_DIM - 3),
        )
        item2 = KnowledgeItem(
            id=id2,
            workspace_id="test_ws",
            title="Cooking Recipes",
            content="Italian pasta and pizza dough recipes.",
            current_revision=rev2,
        )
        await repo.create_item(item2, rev2)

        # Query vector close to Item 1
        query_vec = [1.0, 0.0, 0.0] + [0.0] * (EMBED_DIM - 3)
        results = await repo.search_vector(query_vector=query_vec, workspace_id="test_ws", limit=5)

        assert len(results) == 2
        # Highest similarity item must be rank 1
        assert results[0].id == str(id1)
        assert results[0].title == "Clean Architecture Guide"
        assert results[0].rank == 1
        assert results[0].raw_score > results[1].raw_score
        assert results[1].id == str(id2)
        assert results[1].rank == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_document_repository_lifecycle():
    """Verify DocumentRepository file and chunk persistence."""
    async with TestEnvironment() as env:
        repo = DocumentRepository(session_factory=env.session_factory)

        doc_id = uuid4()
        doc_file = DocumentFile(
            id=doc_id,
            workspace_id="test_ws",
            filename="report.pdf",
            file_path="/storage/test_ws/report.pdf",
            file_size=1024,
            mime_type="application/pdf",
            total_chunks=2,
        )

        # 1. Save document file
        saved_doc = await repo.save_document(doc_file)
        assert saved_doc.id == doc_id
        assert saved_doc.filename == "report.pdf"

        # 2. Save chunks
        chunk1 = DocumentChunk(
            id=uuid4(),
            document_id=doc_id,
            workspace_id="test_ws",
            chunk_index=0,
            content="Page 1 introduction.",
            embedding=[0.1] * EMBED_DIM,
        )
        chunk2 = DocumentChunk(
            id=uuid4(),
            document_id=doc_id,
            workspace_id="test_ws",
            chunk_index=1,
            content="Page 2 conclusions.",
            embedding=[0.2] * EMBED_DIM,
        )
        saved_count = await repo.save_chunks_batch([chunk1, chunk2])
        assert saved_count == 2

        # 3. Retrieve chunks
        chunks = await repo.get_chunks_by_document(doc_id)
        assert len(chunks) == 2
        assert chunks[0].chunk_index == 0
        assert chunks[1].chunk_index == 1

        # 4. Retrieve document metadata
        fetched_doc = await repo.get_by_id(doc_id)
        assert fetched_doc is not None
        assert fetched_doc.total_chunks == 2
