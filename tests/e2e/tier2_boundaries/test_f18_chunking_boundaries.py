"""Tier 2 Boundary Tests for Feature 18: Fixed & Semantic Chunking.

Tests boundary conditions, overlap constraints, short texts, massive continuous strings, and empty inputs.
"""

from typing import List
import pytest


def fixed_chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    """Chunking algorithm enforcing boundary safety."""
    if not text:
        return []

    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")

    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be >= 0 and strictly less than chunk_size")

    chunks = []
    start = 0
    text_len = len(text)
    step = chunk_size - overlap

    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunk = text[start:end]
        chunks.append(chunk)
        if end == text_len:
            break
        start += step

    return chunks


@pytest.mark.tier2
@pytest.mark.feature("F18")
def test_f18_boundary_empty_string_produces_zero_chunks():
    """Test boundary: chunking an empty string returns an empty list."""
    chunks = fixed_chunk_text("", chunk_size=500, overlap=50)
    assert chunks == []


@pytest.mark.tier2
@pytest.mark.feature("F18")
def test_f18_boundary_text_shorter_than_chunk_size():
    """Test boundary: text shorter than chunk_size produces exactly 1 chunk containing entire text."""
    short_text = "Short text under 50 characters."
    chunks = fixed_chunk_text(short_text, chunk_size=500, overlap=50)
    assert len(chunks) == 1
    assert chunks[0] == short_text


@pytest.mark.tier2
@pytest.mark.feature("F18")
def test_f18_boundary_overlap_greater_than_or_equal_to_chunk_size_raises_value_error():
    """Test boundary: configuring overlap >= chunk_size or negative values raises ValueError."""
    # overlap equals chunk_size
    with pytest.raises(ValueError):
        fixed_chunk_text("Some text", chunk_size=100, overlap=100)

    # overlap greater than chunk_size
    with pytest.raises(ValueError):
        fixed_chunk_text("Some text", chunk_size=100, overlap=150)

    # negative chunk_size
    with pytest.raises(ValueError):
        fixed_chunk_text("Some text", chunk_size=-50, overlap=10)

    # negative overlap
    with pytest.raises(ValueError):
        fixed_chunk_text("Some text", chunk_size=100, overlap=-5)


@pytest.mark.tier2
@pytest.mark.feature("F18")
def test_f18_boundary_massive_continuous_string_without_spaces():
    """Test boundary: continuous unbroken character stream splits predictably into chunk slices."""
    unbroken_string = "X" * 2500
    chunks = fixed_chunk_text(unbroken_string, chunk_size=500, overlap=100)
    assert len(chunks) == 6
    assert all(len(c) <= 500 for c in chunks)
    assert chunks[0] == "X" * 500


@pytest.mark.tier2
@pytest.mark.feature("F18")
def test_f18_boundary_single_character_input():
    """Test boundary: single character text produces a single chunk."""
    single_char = "Z"
    chunks = fixed_chunk_text(single_char, chunk_size=500, overlap=50)
    assert len(chunks) == 1
    assert chunks[0] == "Z"
