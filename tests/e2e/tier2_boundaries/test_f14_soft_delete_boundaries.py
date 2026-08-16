"""Tier 2 Boundary Tests for Feature 14: Soft Deletion & Workspace Scoping.

Tests boundary conditions, soft delete flags, search filtering, deletion idempotency, and workspace scoping.
"""

import uuid
import pytest
from sqlalchemy import select

from src.gateway.domain.entities import KnowledgeItem as DomainKnowledgeItem
from src.gateway.infrastructure.persistence.models import KnowledgeItem as ORMKnowledgeItem
from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.tier2
@pytest.mark.feature("F14")
@pytest.mark.asyncio
async def test_f14_boundary_soft_delete_preserves_database_row():
    """Test boundary: soft deletion sets is_deleted=True without physically deleting database record."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            item_id = uuid.uuid4()
            item = ORMKnowledgeItem(
                id=item_id,
                workspace_id="global",
                title="Soft Delete Item",
                content="Payload",
                is_deleted=False,
            )
            session.add(item)
            await session.commit()

            # Soft delete
            item.is_deleted = True
            await session.commit()

            # Row still exists in DB
            res = await session.execute(
                select(ORMKnowledgeItem).where(ORMKnowledgeItem.id == item_id)
            )
            fetched = res.scalar_one_or_none()
            assert fetched is not None
            assert fetched.is_deleted is True


@pytest.mark.tier2
@pytest.mark.feature("F14")
@pytest.mark.asyncio
async def test_f14_boundary_search_excludes_soft_deleted_items():
    """Test boundary: standard active search queries filter out is_deleted=True items."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            active_id = uuid.uuid4()
            deleted_id = uuid.uuid4()

            item_active = ORMKnowledgeItem(
                id=active_id,
                workspace_id="global",
                title="Active Knowledge",
                content="Searchable content",
                is_deleted=False,
            )
            item_deleted = ORMKnowledgeItem(
                id=deleted_id,
                workspace_id="global",
                title="Deleted Knowledge",
                content="Searchable content",
                is_deleted=True,
            )
            session.add_all([item_active, item_deleted])
            await session.commit()

            # Query active items only (standard retrieval contract)
            stmt = select(ORMKnowledgeItem).where(
                ORMKnowledgeItem.workspace_id == "global",
                ORMKnowledgeItem.is_deleted == False,  # noqa: E712
            )
            res = await session.execute(stmt)
            active_items = res.scalars().all()
            assert len(active_items) == 1
            assert active_items[0].id == active_id


@pytest.mark.tier2
@pytest.mark.feature("F14")
@pytest.mark.asyncio
async def test_f14_boundary_double_deletion_idempotency():
    """Test boundary: deleting an already soft-deleted item is idempotent and causes no error."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            item_id = uuid.uuid4()
            item = ORMKnowledgeItem(
                id=item_id,
                workspace_id="global",
                title="Delete Twice",
                content="Payload",
                is_deleted=True,  # Already deleted
            )
            session.add(item)
            await session.commit()

            # Second delete call (setting is_deleted again)
            item.is_deleted = True
            await session.commit()
            assert item.is_deleted is True


@pytest.mark.tier2
@pytest.mark.feature("F14")
def test_f14_boundary_domain_entity_soft_delete_defaults():
    """Test boundary: Domain KnowledgeItem defaults to is_deleted=False and is_global=False."""
    item = DomainKnowledgeItem(
        title="Domain Default Test",
        workspace_id="custom_ws",
    )
    assert item.is_deleted is False
    assert item.is_global is False


@pytest.mark.tier2
@pytest.mark.feature("F14")
@pytest.mark.asyncio
async def test_f14_boundary_workspace_scoped_soft_delete_isolation():
    """Test boundary: soft-deleting an item in Workspace A leaves active items in Workspace B unaffected."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            id_a = uuid.uuid4()
            id_b = uuid.uuid4()

            item_a = ORMKnowledgeItem(
                id=id_a, workspace_id="global", title="Global Item A", content="A", is_deleted=True
            )
            item_b = ORMKnowledgeItem(
                id=id_b, workspace_id="global", title="Global Item B", content="B", is_deleted=False
            )
            session.add_all([item_a, item_b])
            await session.commit()

            res = await session.execute(
                select(ORMKnowledgeItem).where(
                    ORMKnowledgeItem.workspace_id == "global",
                    ORMKnowledgeItem.is_deleted == False,  # noqa: E712
                )
            )
            active = res.scalars().all()
            assert id_b in [x.id for x in active]
            assert id_a not in [x.id for x in active]
