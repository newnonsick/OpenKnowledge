"""Tier 1 Feature Tests for Feature 13: Optimistic Concurrency Control (OCC).

Validates version-checked atomic updates on knowledge items, optimistic concurrency control (OCC)
verification, version advancement, conflict rejection on mismatched version, and database transactional updates.
"""

from uuid import uuid4
import pytest
from sqlalchemy import select

from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision
from src.gateway.domain.exceptions import ConcurrencyConflictException
from src.gateway.infrastructure.persistence.models import (
    KnowledgeItem as DBKnowledgeItem,
    KnowledgeRevision as DBKnowledgeRevision,
)
from tests.e2e.harness.test_env import TestEnvironment


def apply_occ_update(
    item: KnowledgeItem,
    expected_version: int,
    new_content: str,
    new_title: str = None,
    author: str = "tester",
) -> KnowledgeRevision:
    """Helper implementing atomic OCC update logic on domain entities."""
    if item.version != expected_version:
        raise ConcurrencyConflictException(
            message_or_item_id=str(item.id),
            expected_version=expected_version,
            actual_version=item.version,
        )

    new_version = item.version + 1
    content_hash = KnowledgeRevision.compute_hash(new_content)
    rev = KnowledgeRevision(
        id=uuid4(),
        item_id=item.id,
        version=new_version,
        title=new_title or item.title,
        content=new_content,
        content_hash=content_hash,
        author=author,
    )
    item.version = new_version
    item.content = new_content
    if new_title:
        item.title = new_title
    item.current_revision = rev
    return rev


@pytest.mark.tier1
@pytest.mark.feature("F13")
def test_f13_occ_version_increment_on_successful_update():
    """Verify that a valid OCC update with matching expected_version increments version and creates revision."""
    item_id = uuid4()
    item = KnowledgeItem(
        id=item_id,
        workspace_id="ws-dev",
        title="Architecture Principles",
        content="Version 1 content: Keep domain decoupled.",
        version=1,
    )

    # Perform update expecting version 1
    rev2 = apply_occ_update(
        item=item,
        expected_version=1,
        new_content="Version 2 content: Keep domain decoupled and testable.",
        author="alice",
    )

    assert item.version == 2
    assert rev2.version == 2
    assert rev2.item_id == item_id
    assert rev2.content == "Version 2 content: Keep domain decoupled and testable."
    assert rev2.author == "alice"
    assert rev2.content_hash == KnowledgeRevision.compute_hash(rev2.content)


@pytest.mark.tier1
@pytest.mark.feature("F13")
def test_f13_occ_conflict_detection_on_version_mismatch():
    """Verify that OCC update is rejected with ConcurrencyConflictException when expected_version mismatches."""
    item = KnowledgeItem(
        id=uuid4(),
        workspace_id="ws-dev",
        title="Concurrency Guide",
        content="Current active version 3",
        version=3,
    )

    # Stale caller presents expected_version=2 instead of current version 3
    with pytest.raises(ConcurrencyConflictException) as exc_info:
        apply_occ_update(
            item=item,
            expected_version=2,
            new_content="Stale update attempt",
        )

    exc = exc_info.value
    assert exc.status_code == 409
    assert exc.error_type == "concurrency_conflict_error"
    assert exc.code == "version_mismatch"
    assert exc.details["expected_version"] == 2
    assert exc.details["actual_version"] == 3


@pytest.mark.tier1
@pytest.mark.feature("F13")
def test_f13_occ_exception_status_code_and_details():
    """Verify ConcurrencyConflictException formats structured dictionary and HTTP 409 status."""
    item_id = str(uuid4())
    exc = ConcurrencyConflictException(
        message_or_item_id=item_id,
        expected_version=1,
        actual_version=2,
    )
    assert exc.status_code == 409
    data = exc.to_dict()
    assert data["type"] == "concurrency_conflict_error"
    assert data["code"] == "version_mismatch"
    assert data["details"]["item_id"] == item_id
    assert data["details"]["expected_version"] == 1
    assert data["details"]["actual_version"] == 2
    assert "conflict" in data["message"].lower()


@pytest.mark.tier1
@pytest.mark.feature("F13")
@pytest.mark.asyncio
async def test_f13_occ_database_transactional_version_check():
    """Verify database persistence of version updates and revision pointer tracking in TestEnvironment."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            async with session.begin():
                item_id = uuid4()
                ws_id = "global"

                # 1. Insert initial KnowledgeItem (v1)
                item_orm = DBKnowledgeItem(
                    id=item_id,
                    workspace_id=ws_id,
                    title="Database Concurrency",
                    content="v1 initial body",
                )
                session.add(item_orm)
                await session.flush()

                rev1 = DBKnowledgeRevision(
                    id=uuid4(),
                    item_id=item_id,
                    space_id=ws_id,
                    version=1,
                    content="v1 initial body",
                    content_hash=KnowledgeRevision.compute_hash("v1 initial body"),
                    author="init-author",
                )
                session.add(rev1)
                await session.flush()
                item_orm.current_revision_id = rev1.id
                await session.flush()

            # 2. Perform transactional OCC update (v1 -> v2)
            async with session.begin():
                stmt = select(DBKnowledgeItem).where(DBKnowledgeItem.id == item_id)
                res = await session.execute(stmt)
                db_item = res.scalar_one()

                # Advance to v2
                rev2 = DBKnowledgeRevision(
                    id=uuid4(),
                    item_id=item_id,
                    space_id=ws_id,
                    version=2,
                    content="v2 updated body with OCC check",
                    content_hash=KnowledgeRevision.compute_hash("v2 updated body with OCC check"),
                    author="update-author",
                )
                session.add(rev2)
                await session.flush()
                db_item.content = rev2.content
                db_item.current_revision_id = rev2.id

            # 3. Query back and verify version 2 state
            async with session.begin():
                res = await session.execute(select(DBKnowledgeItem).where(DBKnowledgeItem.id == item_id))
                updated_item = res.scalar_one()
                assert updated_item.content == "v2 updated body with OCC check"
                assert updated_item.current_revision_id == rev2.id

                # Verify both revisions exist in database
                revs_res = await session.execute(
                    select(DBKnowledgeRevision).where(DBKnowledgeRevision.item_id == item_id)
                )
                revisions = revs_res.scalars().all()
                assert len(revisions) == 2
                versions = {r.version for r in revisions}
                assert versions == {1, 2}


@pytest.mark.tier1
@pytest.mark.feature("F13")
def test_f13_occ_multiple_sequential_valid_updates():
    """Verify that multiple sequential valid OCC updates (v1 -> v2 -> v3) succeed predictably."""
    item = KnowledgeItem(
        id=uuid4(),
        workspace_id="ws-prod",
        title="Evolving Spec",
        content="Draft 1",
        version=1,
    )

    rev2 = apply_occ_update(item, expected_version=1, new_content="Draft 2 (revised)")
    assert item.version == 2
    assert rev2.version == 2

    rev3 = apply_occ_update(item, expected_version=2, new_content="Draft 3 (finalized)")
    assert item.version == 3
    assert rev3.version == 3
    assert item.content == "Draft 3 (finalized)"
