"""Regression tests for knowledge item creation foreign-key ordering.

Covers the reported bug: ``knowledge_save`` failed on PostgreSQL with
ForeignKeyViolationError because ``knowledge_items`` was inserted with
``current_revision_id`` before the referenced ``knowledge_revisions`` row
existed. SQLite does not enforce FKs by default which is why the suite
passed while production failed; TestEnvironment now enables
``PRAGMA foreign_keys=ON`` for SQLite so this class of bug is caught here.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.gateway.application.services.knowledge_service import KnowledgeService
from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.persistence.knowledge_repository import KnowledgeRepository
from tests.e2e.harness.test_env import TestEnvironment


async def _sqlite_fk_enforced(engine) -> bool:
    async with engine.connect() as conn:
        val = await conn.execute(text("PRAGMA foreign_keys;"))
        return bool(val.scalar())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sqlite_test_env_enforces_foreign_keys():
    """The test harness must enforce FK constraints on SQLite (parity with Postgres)."""
    async with TestEnvironment() as env:
        if env.is_postgres:
            pytest.skip("SQLite-specific FK enforcement check")
        assert await _sqlite_fk_enforced(env.engine) is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_item_respects_circular_foreign_keys():
    """create_item must insert item, then revision, then link pointer (any FK order safe)."""
    async with TestEnvironment() as env:
        repo = KnowledgeRepository(session_factory=env.session_factory)

        item_id = uuid4()
        rev = KnowledgeRevision(
            id=uuid4(),
            item_id=item_id,
            version=1,
            title="FK ordering",
            content="Content that previously triggered FK violation.",
            content_hash=KnowledgeRevision.compute_hash("Content that previously triggered FK violation."),
            author="regression",
        )
        item = KnowledgeItem(
            id=item_id,
            workspace_id="global",
            is_global=True,
            version=1,
            title="FK ordering",
            content="Content that previously triggered FK violation.",
            current_revision=rev,
        )

        saved = await repo.create_item(item, rev)
        assert saved.id == item_id
        assert saved.version == 1

        fetched = await repo.get_item_by_id(item_id)
        assert fetched is not None
        assert fetched.current_revision is not None
        assert fetched.current_revision.id == rev.id
        assert fetched.content == "Content that previously triggered FK violation."


@pytest.mark.integration
@pytest.mark.asyncio
async def test_knowledge_save_tool_end_to_end():
    """The knowledge_save tool flow from the user's report must succeed and persist."""
    async with TestEnvironment() as env:
        repo = KnowledgeRepository(session_factory=env.session_factory)

        class NullEmbeddingClient(HTTPEmbeddingClient):
            """Embedding stub so the test does not depend on an embedding backend."""

            async def embed_texts(self, texts):
                return [[0.0] * 384 for _ in texts]

            async def embed_query(self, query):
                return [0.0] * 384

        service = KnowledgeService(repository=repo, embedding_client=NullEmbeddingClient())

        result = await service.execute_tool(
            tool_call_id="call_regression_1",
            name="knowledge_save",
            arguments={
                "title": "ชื่อของผู้ใช้ (User Name)",
                "content": "ชื่อของผู้ใช้คือ นิว (New)",
                "workspace_id": "global",
            },
            session_workspace_id="global",
        )

        assert result.is_error is False, f"knowledge_save failed: {result.content}"
        payload = json.loads(result.content)
        assert payload["status"] == "created"
        assert payload["title"] == "ชื่อของผู้ใช้ (User Name)"

        # Verify persistence through the read path
        from uuid import UUID as PyUUID

        fetched = await service.get_item(PyUUID(payload["id"]), workspace_id="global")
        assert fetched is not None
        assert fetched.content == "ชื่อของผู้ใช้คือ นิว (New)"
        assert fetched.version == 1

        # Search must find the saved knowledge
        results = await service.search_items("ชื่อ", workspace_id="global")
        assert any(r.id == payload["id"] for r in results)
