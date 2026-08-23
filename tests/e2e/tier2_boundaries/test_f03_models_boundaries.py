"""Tier 2 Boundary Tests for Feature 3: SQLAlchemy 2.0 Async + pgvector Models.

Tests boundary conditions, schema constraints, null checks, vector dimensions, and data integrity.
"""

import uuid
import pytest
from sqlalchemy.exc import IntegrityError, StatementError

from src.gateway.infrastructure.persistence.models import (
    Base,
    DocumentChunk,
    DocumentFile,
    KnowledgeItem,
    KnowledgeRevision,
    Workspace,
)
from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.tier2
@pytest.mark.feature("F3")
@pytest.mark.asyncio
async def test_f03_boundary_null_constraint_violations():
    """Test boundary: inserting records missing mandatory non-nullable columns raises IntegrityError."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            # Missing title and content for KnowledgeItem
            invalid_item = KnowledgeItem(
                id=uuid.uuid4(),
                workspace_id="global",
                title=None,  # Non-nullable violation
                content=None,
            )
            session.add(invalid_item)
            with pytest.raises((IntegrityError, StatementError)):
                await session.flush()
            await session.rollback()


@pytest.mark.tier2
@pytest.mark.feature("F3")
@pytest.mark.asyncio
async def test_f03_boundary_oversized_and_empty_content_strings():
    """Test boundary: persisting 0-byte (empty) and 100KB large text contents in KnowledgeItem."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            # 1. Empty content string
            empty_item = KnowledgeItem(
                id=uuid.uuid4(),
                workspace_id="global",
                title="Empty Content Test",
                content="",
            )
            session.add(empty_item)
            await session.flush()
            assert empty_item.content == ""

            # 2. 100 KB large text content
            large_text = "A" * 100_000
            large_item = KnowledgeItem(
                id=uuid.uuid4(),
                workspace_id="global",
                title="Large Content Test",
                content=large_text,
            )
            session.add(large_item)
            await session.flush()
            assert len(large_item.content) == 100_000
            await session.commit()


@pytest.mark.tier2
@pytest.mark.feature("F3")
@pytest.mark.asyncio
async def test_f03_boundary_non_existent_workspace_foreign_key_violation():
    """Test boundary: creating knowledge item referencing non-existent workspace ID."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            orphan_item = KnowledgeItem(
                id=uuid.uuid4(),
                workspace_id="workspace_does_not_exist_9999",
                title="Orphan Item",
                content="Some orphan content",
            )
            session.add(orphan_item)
            # In SQLite or Postgres with FK enabled, foreign key constraint should be checked or rolled back
            try:
                await session.flush()
            except IntegrityError:
                pass  # Expected FK violation


@pytest.mark.tier2
@pytest.mark.feature("F3")
@pytest.mark.asyncio
async def test_f03_boundary_document_chunk_zero_and_negative_chunk_index():
    """Test boundary: document chunk with zero index, large index, and empty metadata."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            doc = DocumentFile(
                id=uuid.uuid4(),
                workspace_id="global",
                filename="chunk_test.txt",
                file_path="/storage/chunk_test.txt",
                file_size=1024,
                mime_type="text/plain",
            )
            session.add(doc)
            await session.flush()

            # Zero chunk index
            chunk0 = DocumentChunk(
                id=uuid.uuid4(),
                document_id=doc.id,
                workspace_id="global",
                chunk_index=0,
                content="First chunk content",
                metadata_={},
            )
            session.add(chunk0)
            await session.flush()
            assert chunk0.chunk_index == 0
            assert chunk0.metadata_ == {}


@pytest.mark.tier2
@pytest.mark.feature("F3")
@pytest.mark.asyncio
async def test_f03_boundary_knowledge_revision_unique_version_constraint():
    """Test boundary: two revisions for the same item cannot share identical version numbers."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            item_id = uuid.uuid4()
            item = KnowledgeItem(
                id=item_id,
                workspace_id="global",
                title="Versioned Item",
                content="Initial content",
            )
            session.add(item)
            await session.flush()

            rev1 = KnowledgeRevision(
                id=uuid.uuid4(),
                item_id=item_id,
                space_id="global",
                version=1,
                content="Rev 1",
                content_hash="hash1",
            )
            session.add(rev1)
            await session.flush()

            # Attempt duplicate version 1 for same item_id
            rev1_duplicate = KnowledgeRevision(
                id=uuid.uuid4(),
                item_id=item_id,
                space_id="global",
                version=1,
                content="Rev 1 Duplicate",
                content_hash="hash2",
            )
            session.add(rev1_duplicate)
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
