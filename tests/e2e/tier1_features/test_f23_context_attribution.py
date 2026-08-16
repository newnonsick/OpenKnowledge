"""Tier 1 Feature Tests for Feature 23: Structured Context Attribution.

Validates structured context generation with citation headers, source provenance, revision versioning,
workspace scoping, and relevancy score attribution.
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


@pytest.mark.tier1
@pytest.mark.feature("F23")
def test_f23_context_attribution_header_formatting():
    """Verify structured header formatting contains source title, type, version, scope, and score."""
    item = BlendedSearchResult(
        id="k-guidelines",
        source_type="knowledge",
        title="Python Coding Guidelines",
        content="Follow PEP 8 and Clean Architecture.",
        rrf_score=0.032,
        normalized_score=0.95,
        version=2,
        workspace_id="ws-dev",
        is_global=False,
    )

    context_str = format_context_attribution([item])
    assert "[1] Source: Python Coding Guidelines" in context_str
    assert "knowledge" in context_str
    assert "v2" in context_str
    assert "Workspace: ws-dev" in context_str
    assert "Relevancy: 0.95" in context_str
    assert "Follow PEP 8 and Clean Architecture." in context_str


@pytest.mark.tier1
@pytest.mark.feature("F23")
def test_f23_context_snippet_truncation():
    """Verify long snippets are truncated with [truncated] indicator."""
    long_body = "Important architecture concept. " * 50
    item = BlendedSearchResult(
        id="doc_long",
        source_type="document_chunk",
        title="System Design.pdf",
        content=long_body,
        rrf_score=0.03,
        normalized_score=1.0,
        workspace_id="global",
        is_global=True,
    )

    formatted = format_context_attribution([item], max_snippet_len=100)
    assert "... [truncated]" in formatted
    assert "[1] Source: System Design.pdf" in formatted


@pytest.mark.tier1
@pytest.mark.feature("F23")
def test_f23_context_multiple_blended_items_separation():
    """Verify multiple search results are formatted with markdown dividers (---) and sequential indexing."""
    items = [
        BlendedSearchResult(
            id="item1", source_type="knowledge", title="Doc 1", content="Body 1",
            rrf_score=0.032, normalized_score=1.0, workspace_id="global", is_global=True
        ),
        BlendedSearchResult(
            id="item2", source_type="document_chunk", title="Doc 2", content="Body 2",
            rrf_score=0.016, normalized_score=0.5, workspace_id="ws1", is_global=False
        ),
    ]

    formatted = format_context_attribution(items)
    assert "[1] Source: Doc 1" in formatted
    assert "[2] Source: Doc 2" in formatted
    assert "\n\n---\n\n" in formatted


@pytest.mark.tier1
@pytest.mark.feature("F23")
def test_f23_context_empty_results_fallback():
    """Verify empty search results return descriptive fallback message."""
    formatted = format_context_attribution([])
    assert formatted == "No relevant internal knowledge found."


@pytest.mark.tier1
@pytest.mark.feature("F23")
def test_f23_context_attribution_with_custom_metadata():
    """Verify document chunks with metadata include correct source attribution."""
    item = BlendedSearchResult(
        id="chunk-42",
        source_type="document_chunk",
        title="README.md (Chunk #3)",
        content="To run the application, execute `uvicorn src.gateway.main:create_app`.",
        metadata={"file_id": "f-123", "chunk_index": 3},
        rrf_score=0.02,
        normalized_score=0.88,
        workspace_id="ws-infra",
    )
    formatted = format_context_attribution([item])
    assert "README.md (Chunk #3)" in formatted
    assert "document_chunk" in formatted
    assert "0.88" in formatted
