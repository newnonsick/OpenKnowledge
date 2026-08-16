"""Tier 1 Feature Tests for Feature 12: Knowledge Domain Model & SHA-256 Hashing.

Validates KnowledgeRevision SHA-256 hash calculation, version incrementing,
immutable revision history records, and audit metadata.
"""

import hashlib
from uuid import UUID, uuid4
import pytest

from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision


@pytest.mark.tier1
@pytest.mark.feature("F12")
def test_f12_knowledge_revision_sha256_hash_computation():
    """Verify KnowledgeRevision.compute_hash computes exact SHA-256 hex digest."""
    content = "Pragmatic Clean Architecture: Domain at the core."
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    actual_hash = KnowledgeRevision.compute_hash(content)
    assert actual_hash == expected_hash
    assert len(actual_hash) == 64


@pytest.mark.tier1
@pytest.mark.feature("F12")
def test_f12_knowledge_item_creation_defaults():
    """Verify KnowledgeItem creation with initial defaults (version=1, is_deleted=False)."""
    item_id = uuid4()
    item = KnowledgeItem(
        id=item_id,
        workspace_id="ws-dev",
        title="Coding Best Practices",
        content="Write small, testable units of code.",
    )
    assert item.id == item_id
    assert item.version == 1
    assert item.current_version == 1
    assert item.workspace_id == "ws-dev"
    assert item.is_deleted is False
    assert item.is_global is False


@pytest.mark.tier1
@pytest.mark.feature("F12")
def test_f12_immutable_revision_history():
    """Verify creating subsequent revisions retains unique version and content hash."""
    item_id = uuid4()
    rev1 = KnowledgeRevision(
        id=uuid4(),
        item_id=item_id,
        version=1,
        content="Initial draft.",
        content_hash=KnowledgeRevision.compute_hash("Initial draft."),
        author="alice",
    )

    rev2 = KnowledgeRevision(
        id=uuid4(),
        item_id=item_id,
        version=2,
        content="Second revised draft with additional context.",
        content_hash=KnowledgeRevision.compute_hash("Second revised draft with additional context."),
        author="bob",
        change_summary="Expanded context",
    )

    assert rev1.version == 1
    assert rev2.version == 2
    assert rev1.content_hash != rev2.content_hash
    assert rev1.author == "alice"
    assert rev2.author == "bob"


@pytest.mark.tier1
@pytest.mark.feature("F12")
def test_f12_revision_metadata_tracking():
    """Verify tracking tags, timestamps, and change summaries on revisions."""
    rev = KnowledgeRevision(
        item_id=uuid4(),
        version=1,
        content="Content with tags.",
        content_hash=KnowledgeRevision.compute_hash("Content with tags."),
        tags=["architecture", "security", "python"],
        change_summary="Initial commit",
        author="lead-dev",
    )
    assert "architecture" in rev.tags
    assert len(rev.tags) == 3
    assert rev.change_summary == "Initial commit"
    assert rev.created_at is not None


@pytest.mark.tier1
@pytest.mark.feature("F12")
def test_f12_hash_deterministic_equality():
    """Verify hash is deterministic and changes upon minor modifications."""
    text_a = "Deterministic content 123"
    text_b = "Deterministic content 123"
    text_c = "Deterministic content 123 "  # Trailing space

    hash_a = KnowledgeRevision.compute_hash(text_a)
    hash_b = KnowledgeRevision.compute_hash(text_b)
    hash_c = KnowledgeRevision.compute_hash(text_c)

    assert hash_a == hash_b
    assert hash_a != hash_c
