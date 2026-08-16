"""Tier 2 Boundary Tests for Feature 23: Structured Context Attribution.

Tests boundary conditions, citation formatting, empty results, metadata absence, and long snippet truncation.
"""

from typing import List
import pytest
from src.gateway.domain.canonical import BlendedSearchResult


def format_context_attribution(results: List[BlendedSearchResult], max_snippet_len: int = 500) -> str:
    """Formats structured context blocks with citation and provenance attribution."""
    if not results:
        return "No relevant internal knowledge found."

    blocks = []
    for idx, item in enumerate(results, start=1):
        content = item.content
        if len(content) > max_snippet_len:
            content = content[:max_snippet_len].rstrip() + "... [truncated]"

        scope = "Global" if item.is_global else f"Workspace: {item.workspace_id}"
        version_str = f"v{item.version}" if item.version is not None else "latest"
        header = f"[{idx}] Source: {item.title} ({item.source_type}, {version_str}, {scope}, Relevancy: {item.normalized_score:.2f})"
        blocks.append(f"{header}\n{content}")

    return "\n\n---\n\n".join(blocks)


@pytest.mark.tier2
@pytest.mark.feature("F23")
def test_f23_boundary_empty_search_result_attribution():
    """Test boundary: formatting empty search results produces fallback notice."""
    formatted = format_context_attribution([])
    assert "No relevant internal knowledge found." in formatted


@pytest.mark.tier2
@pytest.mark.feature("F23")
def test_f23_boundary_long_document_citation_truncation():
    """Test boundary: contents exceeding max_snippet_len are truncated cleanly with indicator."""
    long_content = "Word " * 500
    res = BlendedSearchResult(
        id="doc_long",
        source_type="document_chunk",
        title="Architecture Guide",
        content=long_content,
        rrf_score=0.03,
        normalized_score=1.0,
        workspace_id="ws_main",
    )
    formatted = format_context_attribution([res], max_snippet_len=200)
    assert "[truncated]" in formatted
    assert "Architecture Guide" in formatted


@pytest.mark.tier2
@pytest.mark.feature("F23")
def test_f23_boundary_missing_metadata_and_version_handling():
    """Test boundary: items with None version and empty metadata render 'latest' and default fields."""
    res = BlendedSearchResult(
        id="doc_unversioned",
        source_type="knowledge",
        title="Unversioned Doc",
        content="Clean content.",
        rrf_score=0.016,
        normalized_score=0.85,
        version=None,
        metadata={},
        is_global=True,
    )
    formatted = format_context_attribution([res])
    assert "latest" in formatted
    assert "Global" in formatted
    assert "0.85" in formatted


@pytest.mark.tier2
@pytest.mark.feature("F23")
def test_f23_boundary_multiple_heterogeneous_sources_attribution():
    """Test boundary: formatting mixed sources (knowledge items and document chunks) preserves index ordering."""
    items = [
        BlendedSearchResult(
            id="k1", source_type="knowledge", title="Coding Standard", content="Content 1",
            rrf_score=0.032, normalized_score=1.0, version=2, workspace_id="ws1"
        ),
        BlendedSearchResult(
            id="d1", source_type="document_chunk", title="README.md", content="Content 2",
            rrf_score=0.016, normalized_score=0.5, version=None, workspace_id="ws1"
        ),
    ]
    formatted = format_context_attribution(items)
    assert "[1] Source: Coding Standard (knowledge, v2, Workspace: ws1" in formatted
    assert "[2] Source: README.md (document_chunk, latest, Workspace: ws1" in formatted
    assert "---" in formatted


@pytest.mark.tier2
@pytest.mark.feature("F23")
def test_f23_boundary_zero_normalized_score_rendering():
    """Test boundary: item with normalized_score = 0.0 formats score correctly without NaN/error."""
    res = BlendedSearchResult(
        id="doc_zero",
        source_type="knowledge",
        title="Zero Score Doc",
        content="Low relevancy",
        rrf_score=0.001,
        normalized_score=0.0,
        workspace_id="global",
    )
    formatted = format_context_attribution([res])
    assert "Relevancy: 0.00" in formatted
