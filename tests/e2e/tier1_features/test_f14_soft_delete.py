"""Tier 1 Feature Tests for Feature 14: Soft Deletion & Workspace Scoping.

Validates soft deletion (is_deleted flag), exclusion of soft-deleted items from standard retrieval,
retention of database records and revision histories, workspace scoping isolation, and global knowledge inheritance.
"""

from uuid import uuid4
import pytest
from sqlalchemy import select

from src.gateway.domain.entities import KnowledgeItem as DomainKnowledgeItem, KnowledgeRevision as DomainKnowledgeRevision
from src.gateway.infrastructure.persistence.models import (
    KnowledgeItem as DBKnowledgeItem,
    KnowledgeRevision as DBKnowledgeRevision,
    Workspace as DBWorkspace,
)
from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.tier1
@pytest.mark.feature("F14")
def test_f14_soft_delete_flag_toggled():
    """Verify soft delete flag toggling on domain KnowledgeItem model."""
    item = DomainKnowledgeItem(
        id=uuid4(),
        workspace_id="ws-main",
        title="Active Item",
        content="Active content body.",
        is_deleted=False,
    )
    assert item.is_deleted is False

    # Mark soft deleted
    item.is_deleted = True
    assert item.is_deleted is True


@pytest.mark.tier1
@pytest.mark.feature("F14")
@pytest.mark.asyncio
async def test_f14_active_query_filters_out_soft_deleted_items():
    """Verify that queries for active items exclude soft-deleted items while keeping rows in database."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            async with session.begin():
                active_id = uuid4()
                deleted_id = uuid4()
                ws_id = "ws-testing"

                item_active = DBKnowledgeItem(
                    id=active_id,
                    workspace_id=ws_id,
                    title="Active Knowledge Item",
                    content="Visible content",
                    is_deleted=False,
                )
                item_deleted = DBKnowledgeItem(
                    id=deleted_id,
                    workspace_id=ws_id,
                    title="Deleted Knowledge Item",
                    content="Hidden content",
                    is_deleted=True,
                )
                session.add(DBWorkspace(id=ws_id, name=f"Workspace {ws_id}"))
                await session.flush()
                session.add_all([item_active, item_deleted])

            # Query with active filter (is_deleted == False)
            async with session.begin():
                stmt = select(DBKnowledgeItem).where(
                    DBKnowledgeItem.workspace_id == ws_id,
                    DBKnowledgeItem.is_deleted == False,  # noqa: E712
                )
                res = await session.execute(stmt)
                active_items = res.scalars().all()
                assert len(active_items) == 1
                assert active_items[0].id == active_id
                assert active_items[0].title == "Active Knowledge Item"

                # Verify deleted item is still present in raw database query
                raw_stmt = select(DBKnowledgeItem).where(DBKnowledgeItem.id == deleted_id)
                raw_res = await session.execute(raw_stmt)
                raw_item = raw_res.scalar_one_or_none()
                assert raw_item is not None
                assert raw_item.is_deleted is True


@pytest.mark.tier1
@pytest.mark.feature("F14")
@pytest.mark.asyncio
async def test_f14_workspace_scoping_isolation():
    """Verify multi-workspace scoping isolates items between distinct workspaces."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            async with session.begin():
                id_alpha = uuid4()
                id_beta = uuid4()

                item_alpha = DBKnowledgeItem(
                    id=id_alpha,
                    workspace_id="ws-alpha",
                    title="Alpha Domain Policy",
                    content="Alpha specific rules",
                    is_deleted=False,
                )
                item_beta = DBKnowledgeItem(
                    id=id_beta,
                    workspace_id="ws-beta",
                    title="Beta Domain Policy",
                    content="Beta specific rules",
                    is_deleted=False,
                )
                session.add(DBWorkspace(id="ws-alpha", name="Workspace Alpha"))
                session.add(DBWorkspace(id="ws-beta", name="Workspace Beta"))
                await session.flush()
                session.add_all([item_alpha, item_beta])

            # Query ws-alpha
            async with session.begin():
                stmt_a = select(DBKnowledgeItem).where(
                    DBKnowledgeItem.workspace_id == "ws-alpha",
                    DBKnowledgeItem.is_deleted == False,  # noqa: E712
                )
                res_a = await session.execute(stmt_a)
                items_a = res_a.scalars().all()
                assert len(items_a) == 1
                assert items_a[0].id == id_alpha

            # Query ws-beta
            async with session.begin():
                stmt_b = select(DBKnowledgeItem).where(
                    DBKnowledgeItem.workspace_id == "ws-beta",
                    DBKnowledgeItem.is_deleted == False,  # noqa: E712
                )
                res_b = await session.execute(stmt_b)
                items_b = res_b.scalars().all()
                assert len(items_b) == 1
                assert items_b[0].id == id_beta


@pytest.mark.tier1
@pytest.mark.feature("F14")
@pytest.mark.asyncio
async def test_f14_global_knowledge_inheritance():
    """Verify global knowledge items (is_global=True or workspace_id='global') can be queried across sessions."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            async with session.begin():
                global_id = uuid4()
                local_id = uuid4()

                item_global = DBKnowledgeItem(
                    id=global_id,
                    workspace_id="global",
                    title="Global Organization Standards",
                    content="Applies everywhere",
                    is_global=True,
                    is_deleted=False,
                )
                item_local = DBKnowledgeItem(
                    id=local_id,
                    workspace_id="ws-team-1",
                    title="Local Team Notes",
                    content="Applies only to Team 1",
                    is_global=False,
                    is_deleted=False,
                )
                session.add(DBWorkspace(id="ws-team-1", name="Workspace Team 1"))
                await session.flush()
                session.add_all([item_global, item_local])

            # Query combined workspace + global items
            async with session.begin():
                stmt = select(DBKnowledgeItem).where(
                    DBKnowledgeItem.workspace_id.in_(["ws-team-1", "global"]),
                    DBKnowledgeItem.is_deleted == False,  # noqa: E712
                )
                res = await session.execute(stmt)
                combined = res.scalars().all()
                assert len(combined) == 2
                ids = {c.id for c in combined}
                assert ids == {global_id, local_id}


@pytest.mark.tier1
@pytest.mark.feature("F14")
@pytest.mark.asyncio
async def test_f14_soft_delete_retains_revision_history():
    """Verify soft-deleting a knowledge item preserves historical KnowledgeRevision records for auditing."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            async with session.begin():
                item_id = uuid4()
                ws_id = "global"

                item = DBKnowledgeItem(
                    id=item_id,
                    workspace_id=ws_id,
                    title="Audited Knowledge Document",
                    content="Version 2 body",
                    is_deleted=False,
                )
                session.add(item)
                await session.flush()

                rev1 = DBKnowledgeRevision(
                    id=uuid4(),
                    item_id=item_id,
                    version=1,
                    content="Version 1 body",
                    content_hash=DomainKnowledgeRevision.compute_hash("Version 1 body"),
                    author="alice",
                )
                rev2 = DBKnowledgeRevision(
                    id=uuid4(),
                    item_id=item_id,
                    version=2,
                    content="Version 2 body",
                    content_hash=DomainKnowledgeRevision.compute_hash("Version 2 body"),
                    author="bob",
                )
                session.add_all([rev1, rev2])
                await session.flush()
                item.current_revision_id = rev2.id

            # Soft delete the item
            async with session.begin():
                stmt = select(DBKnowledgeItem).where(DBKnowledgeItem.id == item_id)
                res = await session.execute(stmt)
                db_item = res.scalar_one()
                db_item.is_deleted = True

            # Verify revisions are still intact in DB
            async with session.begin():
                revs_res = await session.execute(
                    select(DBKnowledgeRevision).where(DBKnowledgeRevision.item_id == item_id)
                )
                revs = revs_res.scalars().all()
                assert len(revs) == 2
                assert {r.author for r in revs} == {"alice", "bob"}
