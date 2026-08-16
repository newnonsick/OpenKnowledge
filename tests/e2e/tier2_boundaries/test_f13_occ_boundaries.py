"""Tier 2 Boundary Tests for Feature 13: Optimistic Concurrency Control (OCC).

Tests boundary conditions, version collision 409, stale writes, negative versions, and concurrent conflicts.
"""

import uuid
import pytest
from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision
from src.gateway.domain.exceptions import ConcurrencyConflictException


def verify_and_apply_occ_update(
    item: KnowledgeItem,
    expected_version: int,
    new_content: str,
    author: str = "user",
) -> KnowledgeRevision:
    """Helper implementing atomic OCC update logic."""
    if item.version != expected_version:
        raise ConcurrencyConflictException(
            message_or_item_id=str(item.id),
            expected_version=expected_version,
            actual_version=item.version,
        )

    new_version = item.version + 1
    content_hash = KnowledgeRevision.compute_hash(new_content)
    rev = KnowledgeRevision(
        item_id=item.id,
        version=new_version,
        content=new_content,
        content_hash=content_hash,
        author=author,
    )
    item.version = new_version
    item.content = new_content
    item.current_revision = rev
    return rev


@pytest.mark.tier2
@pytest.mark.feature("F13")
def test_f13_boundary_stale_expected_version_raises_409_concurrency_conflict():
    """Test boundary: updating item with stale expected_version raises ConcurrencyConflictException (409)."""
    item = KnowledgeItem(
        id=uuid.uuid4(),
        title="Concurrent Doc",
        content="Version 2 content",
        version=2,
    )

    # Caller submits expected_version=1 (stale)
    with pytest.raises(ConcurrencyConflictException) as exc_info:
        verify_and_apply_occ_update(item, expected_version=1, new_content="Conflicting content")

    exc = exc_info.value
    assert exc.status_code == 409
    assert exc.error_type == "concurrency_conflict_error"
    assert exc.details["expected_version"] == 1
    assert exc.details["actual_version"] == 2


@pytest.mark.tier2
@pytest.mark.feature("F13")
def test_f13_boundary_negative_or_zero_expected_version_rejection():
    """Test boundary: negative or zero expected_version on existing version 1 item is rejected."""
    item = KnowledgeItem(id=uuid.uuid4(), title="Doc", content="v1", version=1)

    with pytest.raises(ConcurrencyConflictException):
        verify_and_apply_occ_update(item, expected_version=0, new_content="v0 content")

    with pytest.raises(ConcurrencyConflictException):
        verify_and_apply_occ_update(item, expected_version=-1, new_content="negative version")


@pytest.mark.tier2
@pytest.mark.feature("F13")
def test_f13_boundary_future_non_sequential_expected_version_rejection():
    """Test boundary: submitting a future non-sequential version (e.g. expected_version=5 on version=1) fails."""
    item = KnowledgeItem(id=uuid.uuid4(), title="Doc", content="v1", version=1)

    with pytest.raises(ConcurrencyConflictException) as exc_info:
        verify_and_apply_occ_update(item, expected_version=5, new_content="Future jump")

    assert exc_info.value.status_code == 409
    assert exc_info.value.details["expected_version"] == 5
    assert exc_info.value.details["actual_version"] == 1


@pytest.mark.tier2
@pytest.mark.feature("F13")
def test_f13_boundary_sequential_valid_updates_advance_version():
    """Test boundary: valid sequential updates (v1 -> v2 -> v3) succeed and update current revision."""
    item = KnowledgeItem(id=uuid.uuid4(), title="Doc", content="v1", version=1)

    rev2 = verify_and_apply_occ_update(item, expected_version=1, new_content="v2 content")
    assert item.version == 2
    assert rev2.version == 2

    rev3 = verify_and_apply_occ_update(item, expected_version=2, new_content="v3 content")
    assert item.version == 3
    assert rev3.version == 3
    assert item.content == "v3 content"


@pytest.mark.tier2
@pytest.mark.feature("F13")
def test_f13_boundary_concurrency_conflict_exception_payload_structure():
    """Test boundary: ConcurrencyConflictException formats structured dict with retry instruction."""
    exc = ConcurrencyConflictException(
        message_or_item_id="item-uuid-1234",
        expected_version=1,
        actual_version=2,
    )
    d = exc.to_dict()
    assert d["code"] == "version_mismatch"
    assert d["type"] == "concurrency_conflict_error"
    assert "retry" in d["message"].lower()
