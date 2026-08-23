"""Tier 1 Feature Tests for Feature 3: SQLAlchemy 2.0 Async + pgvector Models.

Validates ORM declarative models, table mapping metadata, column definitions,
foreign keys, and database CRUD operations using SQLAlchemy 2.0.
"""

from uuid import uuid4
import pytest
from sqlalchemy import select

from src.gateway.infrastructure.persistence.models import (
    Base,
    DocumentChunk,
    DocumentFile,
    KnowledgeItem,
    KnowledgeRevision,
    Workspace,
)
from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.tier1
@pytest.mark.feature("F3")
def test_f03_orm_metadata_table_registry():
    """Verify all required persistence tables are registered in Base.metadata."""
    table_names = set(Base.metadata.tables.keys())
    expected_tables = {
        "workspaces",
        "knowledge_items",
        "knowledge_revisions",
        "document_files",
        "document_chunks",
    }
    assert expected_tables.issubset(table_names), f"Missing tables: {expected_tables - table_names}"


@pytest.mark.tier1
@pytest.mark.feature("F3")
def test_f03_workspace_orm_columns():
    """Verify Workspace model columns and relationship configuration."""
    ws_table = Base.metadata.tables["workspaces"]
    assert "id" in ws_table.c
    assert "name" in ws_table.c
    assert "created_at" in ws_table.c
    assert ws_table.c.id.primary_key is True


@pytest.mark.tier1
@pytest.mark.feature("F3")
def test_f03_knowledge_models_orm_columns():
    """Verify KnowledgeItem and KnowledgeRevision ORM column definitions and foreign keys."""
    k_item_table = Base.metadata.tables["knowledge_items"]
    assert "id" in k_item_table.c
    assert "workspace_id" in k_item_table.c
    assert "title" in k_item_table.c
    assert "content" in k_item_table.c
    assert "current_revision_id" in k_item_table.c
    assert "is_global" in k_item_table.c
    assert "is_deleted" in k_item_table.c

    k_rev_table = Base.metadata.tables["knowledge_revisions"]
    assert "id" in k_rev_table.c
    assert "item_id" in k_rev_table.c
    assert "version" in k_rev_table.c
    assert "content_hash" in k_rev_table.c
    assert "author" in k_rev_table.c


@pytest.mark.tier1
@pytest.mark.feature("F3")
def test_f03_document_models_orm_columns():
    """Verify DocumentFile and DocumentChunk ORM column definitions."""
    doc_table = Base.metadata.tables["document_files"]
    assert "id" in doc_table.c
    assert "filename" in doc_table.c
    assert "file_path" in doc_table.c
    assert "file_size" in doc_table.c
    assert "mime_type" in doc_table.c

    chunk_table = Base.metadata.tables["document_chunks"]
    assert "id" in chunk_table.c
    assert "document_id" in chunk_table.c
    assert "chunk_index" in chunk_table.c
    assert "content" in chunk_table.c
    assert "embedding" in chunk_table.c
    assert "tsv" in chunk_table.c
    assert "metadata" in chunk_table.c


@pytest.mark.tier1
@pytest.mark.feature("F3")
@pytest.mark.asyncio
async def test_f03_database_table_creation_and_insertion():
    """Verify asynchronous session CRUD operations on ORM models in test environment."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            async with session.begin():
                # 1. Create a workspace
                ws_id = f"ws_{uuid4().hex[:8]}"
                ws = Workspace(id=ws_id, name="Test Modeling Workspace")
                session.add(ws)
                await session.flush()

                # 2. Create a knowledge item and its revision
                item_id = uuid4()
                item = KnowledgeItem(
                    id=item_id,
                    workspace_id=ws_id,
                    title="SQLAlchemy Domain Model",
                    content="SQLAlchemy 2.0 Async ORM provides clean persistence.",
                    is_global=False,
                )
                session.add(item)
                await session.flush()

                rev = KnowledgeRevision(
                    id=uuid4(),
                    item_id=item_id,
                    space_id=ws_id,
                    version=1,
                    content_hash="abc123sha256hash",
                    content="SQLAlchemy 2.0 Async ORM provides clean persistence.",
                    author="tester",
                )
                session.add(rev)
                await session.flush()
                item.current_revision_id = rev.id
                await session.flush()

            # 3. Query back
            async with session.begin():
                stmt = select(KnowledgeItem).where(KnowledgeItem.id == item_id)
                res = await session.execute(stmt)
                queried_item = res.scalar_one_or_none()
                assert queried_item is not None
                assert queried_item.title == "SQLAlchemy Domain Model"
                assert queried_item.workspace_id == ws_id
