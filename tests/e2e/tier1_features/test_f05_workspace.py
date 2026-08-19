"""Tier 1 Feature Tests for Feature 5: Global Workspace Bootstrapping & Scoping.

Validates that the default 'global' workspace is automatically seeded,
idempotent bootstrapping, workspace scoping, and entity isolation.
"""

from uuid import uuid4
import pytest
from sqlalchemy import select

from src.gateway.domain.entities import KnowledgeItem, Workspace as DomainWorkspace
from src.gateway.infrastructure.persistence.models import KnowledgeItem as DBKnowledgeItem, Workspace as DBWorkspace
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter
from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.tier1
@pytest.mark.feature("F5")
@pytest.mark.asyncio
async def test_f05_global_workspace_bootstrapped_in_db():
    """Verify that the default 'global' workspace is seeded on initialization."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            stmt = select(DBWorkspace).where(DBWorkspace.id == "global")
            result = await session.execute(stmt)
            ws = result.scalar_one_or_none()
            assert ws is not None
            assert ws.id == "global"


@pytest.mark.tier1
@pytest.mark.feature("F5")
@pytest.mark.asyncio
async def test_f05_bootstrap_idempotence():
    """Verify that repeated database setup / cleaning does not duplicate global workspace."""
    async with TestEnvironment() as env:
        await env.clean_database()
        await env.clean_database()
        async with env.session_factory() as session:
            stmt = select(DBWorkspace).where(DBWorkspace.id == "global")
            result = await session.execute(stmt)
            workspaces = result.scalars().all()
            assert len(workspaces) == 1
            assert workspaces[0].id == "global"


@pytest.mark.tier1
@pytest.mark.feature("F5")
@pytest.mark.asyncio
async def test_f05_multi_workspace_isolation():
    """Verify entity isolation across distinct workspace IDs."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            async with session.begin():
                ws1 = DBWorkspace(id="ws-frontend", name="Frontend Team")
                session.add(ws1)
                await session.flush()

                k1 = DBKnowledgeItem(
                    id=uuid4(),
                    workspace_id="ws-frontend",
                    title="React Guidelines",
                    content="Use functional components and hooks.",
                )
                k2 = DBKnowledgeItem(
                    id=uuid4(),
                    workspace_id="ws-backend",
                    title="API Guidelines",
                    content="Follow OpenAPI 3.1 specifications.",
                )
                session.add_all([k1, k2])
                await session.flush()

            # Query items scoped to ws-frontend
            stmt_fe = select(DBKnowledgeItem).where(DBKnowledgeItem.workspace_id == "ws-frontend")
            res_fe = await session.execute(stmt_fe)
            items_fe = res_fe.scalars().all()
            assert len(items_fe) == 1
            assert items_fe[0].title == "React Guidelines"

            # Query items scoped to ws-backend
            stmt_be = select(DBKnowledgeItem).where(DBKnowledgeItem.workspace_id == "ws-backend")
            res_be = await session.execute(stmt_be)
            items_be = res_be.scalars().all()
            assert len(items_be) == 1
            assert items_be[0].title == "API Guidelines"


@pytest.mark.tier1
@pytest.mark.feature("F5")
def test_f05_global_flag_knowledge_items():
    """Verify is_global flag semantics on KnowledgeItem domain model."""
    k_global = KnowledgeItem(
        title="Global Architecture Standards",
        workspace_id="global",
        is_global=True,
    )
    assert k_global.is_global is True
    assert k_global.workspace_id == "global"

    k_local = KnowledgeItem(
        title="Local Module Notes",
        workspace_id="ws-proj-1",
        is_global=False,
    )
    assert k_local.is_global is False
    assert k_local.workspace_id == "ws-proj-1"


@pytest.mark.tier1
@pytest.mark.feature("F5")
@pytest.mark.asyncio
async def test_f05_workspace_directory_containment(temp_storage_dir):
    """Verify LocalStorageAdapter isolates files into per-workspace subdirectories."""
    storage = LocalStorageAdapter(base_dir=temp_storage_dir)
    file_id = "f_12345"

    path_alpha = await storage.save_file("ws-alpha", file_id, "notes.txt", b"Alpha content")
    path_beta = await storage.save_file("ws-beta", file_id, "notes.txt", b"Beta content")

    assert "ws-alpha" in str(path_alpha)
    assert "ws-beta" in str(path_beta)
    assert path_alpha != path_beta

    content_alpha = await storage.read_file("ws-alpha", file_id, "notes.txt")
    content_beta = await storage.read_file("ws-beta", file_id, "notes.txt")
    assert content_alpha == b"Alpha content"
    assert content_beta == b"Beta content"
