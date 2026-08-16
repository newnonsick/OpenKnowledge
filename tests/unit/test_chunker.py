"""Unit tests for text chunking algorithms."""

from __future__ import annotations

import pytest
from src.gateway.application.parsers.chunker import Chunker, fixed_chunk_text


# ==============================================================================
# Fixed Chunker Tests
# ==============================================================================

def test_fixed_chunk_empty_text():
    """Verify empty string returns empty list."""
    assert fixed_chunk_text("", chunk_size=500, overlap=50) == []


def test_fixed_chunk_short_text():
    """Verify text shorter than chunk size returns single element list."""
    text = "Short text."
    chunks = fixed_chunk_text(text, chunk_size=100, overlap=20)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_fixed_chunk_window_and_overlap():
    """Verify sliding window with overlap produces correct slicing."""
    text = "01234567890123456789"  # len = 20
    # chunk_size = 10, overlap = 2, step = 8
    # chunk 0: 0..10
    # chunk 1: 8..18
    # chunk 2: 16..20
    chunks = fixed_chunk_text(text, chunk_size=10, overlap=2)
    assert len(chunks) == 3
    assert chunks[0] == "0123456789"
    assert chunks[1] == "8901234567"
    assert chunks[2] == "6789"


def test_fixed_chunk_invalid_arguments_raise_value_error():
    """Verify argument validation for chunk_size and overlap."""
    with pytest.raises(ValueError):
        fixed_chunk_text("text", chunk_size=0, overlap=0)

    with pytest.raises(ValueError):
        fixed_chunk_text("text", chunk_size=-10, overlap=0)

    with pytest.raises(ValueError):
        fixed_chunk_text("text", chunk_size=100, overlap=-5)

    with pytest.raises(ValueError):
        fixed_chunk_text("text", chunk_size=100, overlap=100)

    with pytest.raises(ValueError):
        fixed_chunk_text("text", chunk_size=100, overlap=120)


def test_fixed_chunk_continuous_string():
    """Verify unbroken character sequence splits into valid bounded slices."""
    continuous = "A" * 1500
    chunks = fixed_chunk_text(continuous, chunk_size=500, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)


# ==============================================================================
# Semantic Chunker Tests
# ==============================================================================

def test_semantic_chunker_empty_input():
    """Verify semantic chunking on empty input returns empty list."""
    chunker = Chunker()
    assert chunker.chunk_semantic("") == []


def test_semantic_chunker_markdown_sections():
    """Verify semantic chunker splits at markdown headings."""
    doc = (
        "# Title 1\n\nContent for section 1 with enough details.\n\n"
        "## Title 2\n\nContent for section 2 with separate topic."
    )
    chunker = Chunker(default_chunk_size=50, default_overlap=0)
    chunks = chunker.chunk_semantic(doc, chunk_size=50, overlap=0)
    assert len(chunks) >= 2
    assert any("Title 1" in c for c in chunks)
    assert any("Title 2" in c for c in chunks)


def test_semantic_chunker_oversized_section_subchunks():
    """Verify semantic chunker subchunks sections exceeding max chunk size."""
    oversized = "Word " * 200  # ~1000 characters
    chunker = Chunker(default_chunk_size=200, default_overlap=20)
    chunks = chunker.chunk_semantic(oversized, chunk_size=200, overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)
