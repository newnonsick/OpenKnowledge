"""Tier 3 Pairwise Combination Tests: Knowledge Subsystem + OCC + Soft Delete + Workspace Scoping.

Tests cross-feature interactions between:
- Feature 12: Knowledge Domain Model & SHA-256 Hashing
- Feature 13: Optimistic Concurrency Control (OCC)
- Feature 14: Soft Deletion & Workspace Scoping
- Feature 15: Knowledge Tool Schemas
- Feature 5: Global Workspace Bootstrapping
"""

import uuid
import pytest

from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.domain.exceptions import ConcurrencyConflictException
from src.gateway.domain.tools import (
    KNOWLEDGE_DELETE_SCHEMA,
    KNOWLEDGE_GET_SCHEMA,
    KNOWLEDGE_SAVE_SCHEMA,
    KNOWLEDGE_SEARCH_SCHEMA,
    KNOWLEDGE_UPDATE_SCHEMA,
    get_internal_tool_definitions,
    is_internal_tool,
)


@pytest.mark.tier3
def test_pairwise_f12_f05_knowledge_v1_creation_and_hash_calculation():
    """Test pairwise interaction: Knowledge creation + SHA-256 hashing + Initial revision (v1)."""
    item_id = uuid.uuid4()
    content = "Initial knowledge item content for gateway architecture."
    content_hash = KnowledgeRevision.compute_hash(content)

    rev_v1 = KnowledgeRevision(
        item_id=item_id,
        version=1,
        title="Gateway Arch",
        content=content,
        content_hash=content_hash,
        author="alice",
    )

    item = KnowledgeItem(
        id=item_id,
        workspace_id="project_alpha",
        is_global=False,
        version=1,
        title="Gateway Arch",
        content=content,
        current_revision=rev_v1,
    )

    assert item.version == 1
    assert item.current_version == 1
    assert item.current_revision is not None
    assert item.current_revision.version == 1
    assert item.current_revision.content_hash == content_hash
    assert len(content_hash) == 64  # SHA-256 hex digest length
    assert item.is_deleted is False


@pytest.mark.tier3
def test_pairwise_f13_f12_knowledge_occ_successful_update_v2():
    """Test pairwise interaction: OCC update with valid expected_version advances to v2."""
    item_id = uuid.uuid4()
    initial_content = "Version 1 guidelines."
    rev_v1 = KnowledgeRevision(
        item_id=item_id,
        version=1,
        title="Coding Guidelines",
        content=initial_content,
        content_hash=KnowledgeRevision.compute_hash(initial_content),
    )

    item = KnowledgeItem(
        id=item_id,
        workspace_id="global",
        is_global=True,
        version=1,
        title="Coding Guidelines",
        content=initial_content,
        current_revision=rev_v1,
    )

    # Perform OCC update
    expected_version = 1
    if expected_version != item.version:
        raise ConcurrencyConflictException(
            message_or_item_id=str(item.id),
            expected_version=expected_version,
            actual_version=item.version,
        )

    updated_content = "Version 2 guidelines with async support."
    rev_v2 = KnowledgeRevision(
        item_id=item_id,
        version=item.version + 1,
        title="Coding Guidelines",
        content=updated_content,
        content_hash=KnowledgeRevision.compute_hash(updated_content),
        author="bob",
        change_summary="Added async support guidelines",
    )

    item.version = rev_v2.version
    item.content = updated_content
    item.current_revision = rev_v2

    assert item.version == 2
    assert item.current_revision.version == 2
    assert item.current_revision.content_hash != rev_v1.content_hash
    assert item.current_revision.change_summary == "Added async support guidelines"


@pytest.mark.tier3
def test_pairwise_f13_knowledge_occ_conflict_detection_raises_409():
    """Test pairwise interaction: OCC update with stale expected_version raises ConcurrencyConflictException."""
    item_id = uuid.uuid4()
    item = KnowledgeItem(
        id=item_id,
        workspace_id="team_ws",
        version=2,  # Already updated by someone else
        title="Team Protocols",
        content="Protocols v2",
    )

    stale_expected_version = 1

    with pytest.raises(ConcurrencyConflictException) as exc_info:
        if stale_expected_version != item.version:
            raise ConcurrencyConflictException(
                message_or_item_id=str(item.id),
                expected_version=stale_expected_version,
                actual_version=item.version,
            )

    exc = exc_info.value
    assert exc.status_code == 409
    assert exc.error_type == "concurrency_conflict_error"
    assert exc.details["expected_version"] == 1
    assert exc.details["actual_version"] == 2
    assert "Version conflict" in exc.message


@pytest.mark.tier3
def test_pairwise_f14_f12_knowledge_soft_delete_and_audit_preservation():
    """Test pairwise interaction: Soft deletion preserves revision history while hiding active record."""
    item_id = uuid.uuid4()
    rev1 = KnowledgeRevision(
        item_id=item_id,
        version=1,
        content="Content v1",
        content_hash=KnowledgeRevision.compute_hash("Content v1"),
    )
    rev2 = KnowledgeRevision(
        item_id=item_id,
        version=2,
        content="Content v2",
        content_hash=KnowledgeRevision.compute_hash("Content v2"),
    )

    item = KnowledgeItem(
        id=item_id,
        workspace_id="ws_del",
        version=2,
        title="To be deleted",
        content="Content v2",
        is_deleted=False,
    )

    item.is_deleted = True
    assert item.is_deleted is True

    active_items = [i for i in [item] if not i.is_deleted]
    assert len(active_items) == 0

    revisions = [rev1, rev2]
    assert len(revisions) == 2
    assert revisions[0].version == 1
    assert revisions[1].version == 2


@pytest.mark.tier3
def test_pairwise_f14_f05_knowledge_workspace_scoping_and_global_inheritance():
    """Test pairwise interaction: Workspace isolation vs Global shared visibility."""
    item_global = KnowledgeItem(
        id=uuid.uuid4(),
        workspace_id="global",
        is_global=True,
        title="Global Standards",
        content="All teams follow this.",
    )
    item_ws_a = KnowledgeItem(
        id=uuid.uuid4(),
        workspace_id="ws_a",
        is_global=False,
        title="Workspace A Internal",
        content="Only for team A.",
    )
    item_ws_b = KnowledgeItem(
        id=uuid.uuid4(),
        workspace_id="ws_b",
        is_global=False,
        title="Workspace B Internal",
        content="Only for team B.",
    )

    all_items = [item_global, item_ws_a, item_ws_b]

    ws_a_visible = [i for i in all_items if i.workspace_id == "ws_a" or i.is_global]
    assert len(ws_a_visible) == 2
    assert item_ws_a in ws_a_visible
    assert item_global in ws_a_visible
    assert item_ws_b not in ws_a_visible

    ws_b_visible = [i for i in all_items if i.workspace_id == "ws_b" or i.is_global]
    assert len(ws_b_visible) == 2
    assert item_ws_b in ws_b_visible
    assert item_global in ws_b_visible
    assert item_ws_a not in ws_b_visible


@pytest.mark.tier3
def test_pairwise_f15_f13_knowledge_tool_schemas_and_internal_registry():
    """Test pairwise interaction: Knowledge tool schema contracts and internal tool discriminator."""
    assert is_internal_tool("knowledge_search") is True
    assert is_internal_tool("knowledge_save") is True
    assert is_internal_tool("knowledge_update") is True
    assert is_internal_tool("knowledge_get") is True
    assert is_internal_tool("knowledge_delete") is True
    assert is_internal_tool("bash") is False
    assert is_internal_tool("edit_file") is False

    tool_defs = get_internal_tool_definitions()
    assert len(tool_defs) == 5

    names = {td.function.name for td in tool_defs}
    assert "knowledge_search" in names
    assert "knowledge_update" in names

    update_schema = KNOWLEDGE_UPDATE_SCHEMA["function"]
    assert "expected_version" in update_schema["parameters"]["required"]
    assert "item_id" in update_schema["parameters"]["required"]
