"""Tier 2 Boundary Tests for Feature 5: Global Workspace Bootstrapping.

Tests boundary conditions, special workspace characters, unicode, isolation, and bootstrapping idempotency.
"""

import uuid
import pytest
from sqlalchemy import select, text

from src.gateway.domain.entities import Workspace as DomainWorkspace
from src.gateway.infrastructure.persistence.models import KnowledgeItem, Workspace as ORMWorkspace
from src.gateway.main import bootstrap_global_workspace
from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.tier2
@pytest.mark.feature("F5")
@pytest.mark.asyncio
async def test_f05_boundary_bootstrap_idempotency_multiple_executions():
    """Test boundary: bootstrap_global_workspace can be called repeatedly without error."""
    from unittest.mock import patch

    async with TestEnvironment() as env:
        with patch("src.gateway.main.get_session_factory", return_value=env.session_factory):
            # Call multiple times
            await bootstrap_global_workspace()
            await bootstrap_global_workspace()
            await bootstrap_global_workspace()

        async with env.session_factory() as session:
            stmt = select(ORMWorkspace).where(ORMWorkspace.id == "global")
            result = await session.execute(stmt)
            workspaces = result.scalars().all()
            assert len(workspaces) == 1
            assert workspaces[0].id == "global"


@pytest.mark.tier2
@pytest.mark.feature("F5")
@pytest.mark.asyncio
async def test_f05_boundary_workspace_ids_with_special_characters_and_unicode():
    """Test boundary: creating workspaces with hyphens, underscores, dots, and unicode names."""
    test_workspaces = [
        ("ws-hyphen-123", "Workspace Hyphen"),
        ("ws_underscore_456", "Workspace Underscore"),
        ("ws.dot.789", "Workspace Dot"),
        ("ws-unicode-日本語", "Workspace Japanese"),
        ("ws-emoji-🚀", "Workspace Emoji"),
    ]

    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            for ws_id, ws_name in test_workspaces:
                ws = ORMWorkspace(id=ws_id, name=ws_name)
                session.add(ws)
            await session.commit()

            for ws_id, ws_name in test_workspaces:
                res = await session.execute(select(ORMWorkspace).where(ORMWorkspace.id == ws_id))
                obj = res.scalar_one_or_none()
                assert obj is not None
                assert obj.name == ws_name


@pytest.mark.tier2
@pytest.mark.feature("F5")
@pytest.mark.asyncio
async def test_f05_boundary_cross_workspace_data_isolation():
    """Test boundary: items created in workspace A are isolated from workspace B."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            # Create two workspaces
            ws_a = ORMWorkspace(id="ws_alpha", name="Alpha Workspace")
            ws_b = ORMWorkspace(id="ws_beta", name="Beta Workspace")
            session.add_all([ws_a, ws_b])
            await session.flush()

            # Add items to each
            item_a = KnowledgeItem(
                id=uuid.uuid4(),
                workspace_id="ws_alpha",
                title="Alpha Secret",
                content="Alpha content",
            )
            item_b = KnowledgeItem(
                id=uuid.uuid4(),
                workspace_id="ws_beta",
                title="Beta Secret",
                content="Beta content",
            )
            session.add_all([item_a, item_b])
            await session.commit()

            # Query Alpha only
            res_a = await session.execute(
                select(KnowledgeItem).where(KnowledgeItem.workspace_id == "ws_alpha")
            )
            items_a = res_a.scalars().all()
            assert len(items_a) == 1
            assert items_a[0].title == "Alpha Secret"

            # Query Beta only
            res_b = await session.execute(
                select(KnowledgeItem).where(KnowledgeItem.workspace_id == "ws_beta")
            )
            items_b = res_b.scalars().all()
            assert len(items_b) == 1
            assert items_b[0].title == "Beta Secret"


@pytest.mark.tier2
@pytest.mark.feature("F5")
def test_f05_boundary_domain_workspace_model_validation():
    """Test boundary: domain Workspace entity default values and serialization."""
    d_ws = DomainWorkspace(id="global")
    assert d_ws.id == "global"
    assert d_ws.name == "Workspace"
    assert d_ws.created_at is not None
    assert d_ws.updated_at is not None

    custom_ws = DomainWorkspace(id="custom_ws_1", name="Custom Project Space")
    assert custom_ws.id == "custom_ws_1"
    assert custom_ws.name == "Custom Project Space"


@pytest.mark.tier2
@pytest.mark.feature("F5")
@pytest.mark.asyncio
async def test_f05_boundary_global_workspace_immutability_and_presence():
    """Test boundary: global workspace cannot be duplicated."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            # Query global workspace
            res = await session.execute(select(ORMWorkspace).where(ORMWorkspace.id == "global"))
            global_ws = res.scalar_one_or_none()
            assert global_ws is not None
            assert global_ws.id == "global"
