"""Tier 2 Boundary Tests for Feature 12: Knowledge Domain Model & SHA-256 Hashing.

Tests boundary conditions, content hashing, unicode characters, 0-byte content, and revision integrity.
"""

import hashlib
import uuid
import pytest

from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision


@pytest.mark.tier2
@pytest.mark.feature("F12")
def test_f12_boundary_zero_byte_content_hash_calculation():
    """Test boundary: SHA-256 computation on empty string matches known standard digest e3b0c44..."""
    empty_digest = KnowledgeRevision.compute_hash("")
    expected_empty_sha256 = hashlib.sha256(b"").hexdigest()
    assert empty_digest == expected_empty_sha256
    assert empty_digest == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    rev = KnowledgeRevision(
        item_id=uuid.uuid4(),
        version=1,
        content="",
        content_hash=empty_digest,
    )
    assert rev.content_hash == empty_digest


@pytest.mark.tier2
@pytest.mark.feature("F12")
def test_f12_boundary_unicode_emojis_and_multibyte_characters():
    """Test boundary: SHA-256 hashing and entity handling with unicode emojis, CJK, and RTL scripts."""
    unicode_content = "# Knowledge Title 🌟\n\n日本語のテキスト & עִבְרִית & 🚀🤖"
    computed_hash = KnowledgeRevision.compute_hash(unicode_content)
    assert computed_hash == hashlib.sha256(unicode_content.encode("utf-8")).hexdigest()

    item = KnowledgeItem(
        title="Unicode 🌟 Knowledge",
        content=unicode_content,
        workspace_id="ws_unicode",
        tags=["tag1", "日本語", "🚀"],
    )
    assert item.title == "Unicode 🌟 Knowledge"
    assert "日本語" in item.tags


@pytest.mark.tier2
@pytest.mark.feature("F12")
def test_f12_boundary_identical_content_produces_identical_sha256_hash():
    """Test boundary: identical content produces consistent hash across multiple evaluations."""
    content = "Deterministic content body for SHA-256 verification."
    hash1 = KnowledgeRevision.compute_hash(content)
    hash2 = KnowledgeRevision.compute_hash(content)
    assert hash1 == hash2

    # Slight difference produces completely different digest
    altered_hash = KnowledgeRevision.compute_hash(content + " ")
    assert hash1 != altered_hash


@pytest.mark.tier2
@pytest.mark.feature("F12")
def test_f12_boundary_massive_markdown_content_hashing():
    """Test boundary: computing SHA-256 hash on a 1MB large markdown document."""
    large_markdown = "# Heading\n\n" + ("Lorem ipsum dolor sit amet. " * 40_000)
    digest = KnowledgeRevision.compute_hash(large_markdown)
    assert len(digest) == 64
    assert digest == hashlib.sha256(large_markdown.encode("utf-8")).hexdigest()


@pytest.mark.tier2
@pytest.mark.feature("F12")
def test_f12_boundary_revision_version_progression_and_audit():
    """Test boundary: KnowledgeItem revision history progression and immutability tracking."""
    item_id = uuid.uuid4()
    item = KnowledgeItem(
        id=item_id,
        title="Document Lifecycle",
        workspace_id="global",
        version=1,
    )
    assert item.current_version == 1

    # Advance version
    item.current_version = 2
    assert item.version == 2
