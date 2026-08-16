"""Tier 4 Real-World Scenario: Collaborative Team Knowledge Editing with OCC.

Simulates collaborative multi-developer/agent editing of a shared knowledge item:
- Alice and Bob read v1 simultaneously.
- Alice updates with expected_version=1 -> succeeds (v2).
- Bob tries update with expected_version=1 -> fails with 409 OCC conflict.
- Bob fetches v2, merges changes, and updates with expected_version=2 -> succeeds (v3).
- Full audit history and SHA-256 integrity verified.
"""

import uuid
import pytest

from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision
from src.gateway.domain.exceptions import ConcurrencyConflictException


class InMemoryKnowledgeService:
    """Simulated knowledge repository service enforcing OCC and revision history."""

    def __init__(self):
        self.items: dict[uuid.UUID, KnowledgeItem] = {}
        self.revisions: dict[uuid.UUID, list[KnowledgeRevision]] = {}

    def create(self, title: str, content: str, author: str, workspace_id: str = "global") -> KnowledgeItem:
        item_id = uuid.uuid4()
        content_hash = KnowledgeRevision.compute_hash(content)
        rev = KnowledgeRevision(
            item_id=item_id,
            version=1,
            title=title,
            content=content,
            content_hash=content_hash,
            author=author,
        )
        item = KnowledgeItem(
            id=item_id,
            workspace_id=workspace_id,
            is_global=(workspace_id == "global"),
            version=1,
            title=title,
            content=content,
            current_revision=rev,
        )
        self.items[item_id] = item
        self.revisions[item_id] = [rev]
        return item

    def get(self, item_id: uuid.UUID) -> KnowledgeItem:
        return self.items[item_id]

    def update(
        self,
        item_id: uuid.UUID,
        expected_version: int,
        content: str,
        author: str,
        change_summary: str,
    ) -> KnowledgeItem:
        item = self.items[item_id]
        if expected_version != item.version:
            raise ConcurrencyConflictException(
                message_or_item_id=str(item_id),
                expected_version=expected_version,
                actual_version=item.version,
            )

        new_version = item.version + 1
        content_hash = KnowledgeRevision.compute_hash(content)
        rev = KnowledgeRevision(
            item_id=item_id,
            version=new_version,
            title=item.title,
            content=content,
            content_hash=content_hash,
            author=author,
            change_summary=change_summary,
        )
        item.version = new_version
        item.content = content
        item.current_revision = rev
        self.revisions[item_id].append(rev)
        return item


@pytest.mark.tier4
def test_scenario_f12_f13_collaborative_knowledge_editing_occ_conflict_and_resolution():
    """Scenario: Alice and Bob concurrent edit -> OCC 409 rejection -> Re-read v2 and resolve -> v3 created."""
    service = InMemoryKnowledgeService()

    initial_doc = (
        "# Database Migration Policy\n\n"
        "1. All migrations must be written in Alembic.\n"
        "2. Run migrations automatically during startup lifespan."
    )
    item = service.create(
        title="Database Migration Policy",
        content=initial_doc,
        author="system",
    )
    item_id = item.id
    assert item.version == 1

    alice_view = service.get(item_id)
    bob_view = service.get(item_id)
    assert alice_view.version == 1
    assert bob_view.version == 1

    alice_update_content = (
        initial_doc + "\n3. pgvector extension must be verified before running DDL."
    )
    item_v2 = service.update(
        item_id=item_id,
        expected_version=1,
        content=alice_update_content,
        author="alice",
        change_summary="Added pgvector extension requirement",
    )
    assert item_v2.version == 2
    assert service.get(item_id).version == 2

    bob_update_content = (
        initial_doc + "\n4. Rollback scripts must be tested in staging."
    )
    with pytest.raises(ConcurrencyConflictException) as exc_info:
        service.update(
            item_id=item_id,
            expected_version=1,
            content=bob_update_content,
            author="bob",
            change_summary="Added rollback script rule",
        )

    conflict_exc = exc_info.value
    assert conflict_exc.status_code == 409
    assert conflict_exc.details["expected_version"] == 1
    assert conflict_exc.details["actual_version"] == 2

    latest_v2 = service.get(item_id)
    merged_content = (
        latest_v2.content + "\n4. Rollback scripts must be tested in staging."
    )
    item_v3 = service.update(
        item_id=item_id,
        expected_version=2,
        content=merged_content,
        author="bob",
        change_summary="Merged rollback script rule with Alice's pgvector rule",
    )
    assert item_v3.version == 3

    history = service.revisions[item_id]
    assert len(history) == 3
    assert history[0].version == 1
    assert history[0].author == "system"
    assert history[1].version == 2
    assert history[1].author == "alice"
    assert history[2].version == 3
    assert history[2].author == "bob"

    hashes = [h.content_hash for h in history]
    assert len(set(hashes)) == 3
    assert "pgvector" in item_v3.content
    assert "Rollback scripts" in item_v3.content


@pytest.mark.tier4
def test_scenario_f13_collaborative_knowledge_editing_sequential_agent_updates():
    """Scenario: Multiple coding agents performing sequential updates on global design doc."""
    service = InMemoryKnowledgeService()
    item = service.create("System Arch", "Base architecture", author="agent_0")

    for i in range(1, 5):
        current_version = service.get(item.id).version
        service.update(
            item_id=item.id,
            expected_version=current_version,
            content=f"Architecture revision {i}",
            author=f"agent_{i}",
            change_summary=f"Iteration {i}",
        )

    final_item = service.get(item.id)
    assert final_item.version == 5
    assert len(service.revisions[item.id]) == 5
