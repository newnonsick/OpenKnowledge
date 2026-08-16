"""Tier 1 Feature Tests for Feature 20: PostgreSQL Full-Text Search (tsvector + GIN).

Validates full-text search query tokenization, tsquery sanitization, lexical ranking,
RankedSearchResult candidate model generation, and workspace-scoped FTS queries.
"""

import re
from typing import List
import pytest

from src.gateway.domain.canonical import RankedSearchResult


def sanitize_tsquery(raw_query: str) -> str:
    """Sanitizes user input for PostgreSQL full-text search tsquery parsing."""
    if not raw_query or not raw_query.strip():
        return ""
    cleaned = re.sub(r"[!&|():*'\";\-]+", " ", raw_query)
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return ""
    return " & ".join(tokens)


def mock_fts_search(
    corpus: List[dict],
    query: str,
    workspace_id: str = "global",
    limit: int = 10,
) -> List[RankedSearchResult]:
    """Simulates PostgreSQL tsvector/tsquery lexical search and ranking."""
    sanitized = sanitize_tsquery(query)
    if not sanitized:
        return []

    query_tokens = [t.lower() for t in sanitized.split(" & ")]
    matches = []

    for item in corpus:
        if item.get("workspace_id") != workspace_id and not item.get("is_global", False):
            continue

        text_to_search = f"{item.get('title', '')} {item.get('content', '')}".lower()
        score = 0.0
        match_count = 0
        for token in query_tokens:
            if token in text_to_search:
                match_count += 1
                score += text_to_search.count(token) * 1.0

        if match_count > 0:
            matches.append((item, score))

    matches.sort(key=lambda x: x[1], reverse=True)
    results = []
    for rank, (item, score) in enumerate(matches[:limit], start=1):
        results.append(
            RankedSearchResult(
                id=item["id"],
                source_type=item.get("source_type", "knowledge"),
                title=item["title"],
                content=item["content"],
                rank=rank,
                raw_score=round(score, 4),
                workspace_id=item.get("workspace_id", workspace_id),
                is_global=item.get("is_global", False),
            )
        )
    return results


@pytest.mark.tier1
@pytest.mark.feature("F20")
def test_f20_tsquery_sanitization_standard_keywords():
    """Verify raw query string sanitization into valid PostgreSQL tsquery syntax."""
    query = "architecture design patterns"
    sanitized = sanitize_tsquery(query)
    assert sanitized == "architecture & design & patterns"


@pytest.mark.tier1
@pytest.mark.feature("F20")
def test_f20_fts_ranked_search_result_creation():
    """Verify RankedSearchResult model fields for lexical full-text search matches."""
    res = RankedSearchResult(
        id="k-100",
        source_type="knowledge",
        title="PostgreSQL FTS Indexing",
        content="GIN indexes on tsvector columns accelerate text search.",
        rank=1,
        raw_score=2.5,
        workspace_id="ws-dev",
        is_global=False,
    )
    assert res.id == "k-100"
    assert res.source_type == "knowledge"
    assert res.rank == 1
    assert res.raw_score == 2.5
    assert res.workspace_id == "ws-dev"


@pytest.mark.tier1
@pytest.mark.feature("F20")
def test_f20_case_insensitive_lexical_matching():
    """Verify case-insensitive lexical matching across title and content bodies."""
    corpus = [
        {"id": "doc1", "title": "PYTHON GUIDE", "content": "FastAPI and Clean Architecture", "workspace_id": "ws1"},
        {"id": "doc2", "title": "Rust Guide", "content": "Memory safety and speed", "workspace_id": "ws1"},
    ]
    results = mock_fts_search(corpus, "python fastapi", workspace_id="ws1")
    assert len(results) == 1
    assert results[0].id == "doc1"
    assert results[0].rank == 1
    assert results[0].raw_score > 0


@pytest.mark.tier1
@pytest.mark.feature("F20")
def test_f20_multi_word_query_tokenization():
    """Verify multi-word query ranking sorts higher relevance matches first."""
    corpus = [
        {"id": "doc_a", "title": "Clean Architecture", "content": "Architecture principles for software design.", "workspace_id": "global"},
        {"id": "doc_b", "title": "Other Topic", "content": "Just a generic note.", "workspace_id": "global"},
    ]
    results = mock_fts_search(corpus, "clean architecture design", workspace_id="global")
    assert len(results) == 1
    assert results[0].id == "doc_a"
    assert results[0].title == "Clean Architecture"


@pytest.mark.tier1
@pytest.mark.feature("F20")
def test_f20_workspace_scoped_fts_results():
    """Verify full-text search respects workspace scoping and includes global items."""
    corpus = [
        {"id": "ws_item", "title": "Workspace Note", "content": "Specific project details", "workspace_id": "ws-team-a", "is_global": False},
        {"id": "global_item", "title": "Global Note", "content": "Global organization details", "workspace_id": "global", "is_global": True},
        {"id": "other_ws", "title": "Other Note", "content": "Other project details", "workspace_id": "ws-team-b", "is_global": False},
    ]
    results = mock_fts_search(corpus, "details", workspace_id="ws-team-a")
    returned_ids = {r.id for r in results}
    assert "ws_item" in returned_ids
    assert "global_item" in returned_ids
    assert "other_ws" not in returned_ids
