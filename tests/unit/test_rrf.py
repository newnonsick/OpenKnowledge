"""Unit tests for Reciprocal Rank Fusion (RRF) and context attribution formatting."""

import pytest
from src.gateway.application.services.rrf import (
    DEFAULT_RRF_K,
    compute_rrf,
    compute_rrf_fusion,
    compute_rrf_score,
    format_citation,
    format_context_attribution,
    sanitize_tsquery,
)
from src.gateway.domain.canonical import BlendedSearchResult, RankedSearchResult


def _make_candidate(
    doc_id: str,
    title: str = "Test Doc",
    content: str = "Test Content",
    rank: int = 1,
    raw_score: float = 1.0,
    source_type: str = "knowledge",
    workspace_id: str = "global",
    is_global: bool = True,
    version: int = 1,
) -> RankedSearchResult:
    """Helper to create RankedSearchResult instance for testing."""
    return RankedSearchResult(
        id=doc_id,
        source_type=source_type,  # type: ignore[arg-type]
        title=title,
        content=content,
        rank=rank,
        raw_score=raw_score,
        workspace_id=workspace_id,
        is_global=is_global,
        version=version,
    )


# ---------------------------------------------------------------------------
# 1. sanitize_tsquery Tests
# ---------------------------------------------------------------------------


def test_sanitize_tsquery_standard_tokens():
    """Verify standard space-separated keywords are joined with '&'."""
    assert sanitize_tsquery("clean architecture patterns") == "clean & architecture & patterns"


def test_sanitize_tsquery_empty_and_whitespace():
    """Verify empty and whitespace strings return empty string."""
    assert sanitize_tsquery("") == ""
    assert sanitize_tsquery("   ") == ""
    assert sanitize_tsquery("\t\r\n") == ""


def test_sanitize_tsquery_punctuation_stripped():
    """Verify dangerous tsquery control operators are stripped."""
    dirty = "fastapi & (python | rust)!: *'\" ; --test"
    sanitized = sanitize_tsquery(dirty)
    for forbidden in ["!", "&", "|", "(", ")", ":", "*", "'", '"', ";", "--"]:
        # Only single '&' used as join operator is permitted
        pass
    assert sanitized == "fastapi & python & rust & test"


def test_sanitize_tsquery_unicode():
    """Verify international character sets are preserved."""
    assert sanitize_tsquery("機械学習 postgresql 🐍") == "機械学習 & postgresql & 🐍"


# ---------------------------------------------------------------------------
# 2. compute_rrf Core Algorithm Tests
# ---------------------------------------------------------------------------


def test_compute_rrf_single_item_formula_k60():
    """Verify single candidate score is exactly 1 / (60 + rank)."""
    item = _make_candidate(doc_id="item1", rank=1)
    results = compute_rrf([[item]], rrf_k=60)
    assert len(results) == 1

    expected_rrf = round(1.0 / 61.0, 6)
    assert results[0].rrf_score == expected_rrf
    assert results[0].normalized_score == 1.0
    assert results[0].fts_rank == 1
    assert results[0].vector_rank is None


def test_compute_rrf_custom_k_value():
    """Verify custom k values are used in reciprocal formula."""
    item1 = _make_candidate(doc_id="item_k10_1", rank=1)
    item2 = _make_candidate(doc_id="item_k10_2", rank=2)
    # k=10, rank=1 -> 1 / (10 + 1) = 1/11 = 0.090909
    # k=10, rank=2 -> 1 / (10 + 2) = 1/12 = 0.083333
    results = compute_rrf([[item1, item2]], rrf_k=10)
    assert len(results) == 2
    assert results[0].rrf_score == round(1.0 / 11.0, 6)
    assert results[1].rrf_score == round(1.0 / 12.0, 6)


def test_compute_rrf_invalid_k_fallback():
    """Verify k <= 0 defaults to 60."""
    item = _make_candidate(doc_id="fallback", rank=1)
    res_k0 = compute_rrf([[item]], rrf_k=0)
    res_kneg = compute_rrf([[item]], rrf_k=-10)

    expected = round(1.0 / 61.0, 6)
    assert res_k0[0].rrf_score == expected
    assert res_kneg[0].rrf_score == expected


def test_compute_rrf_weights_scaling():
    """Verify list weights scale the respective RRF component scores."""
    item_fts = _make_candidate(doc_id="doc1", rank=1)
    item_vec = _make_candidate(doc_id="doc2", rank=1)

    # Weight FTS 2.0, Vector 0.5
    results = compute_rrf(
        ranked_lists=[[item_fts], [item_vec]],
        weights=[2.0, 0.5],
        rrf_k=60,
    )
    assert len(results) == 2
    # doc1 score: 2.0 / 61
    # doc2 score: 0.5 / 61
    assert results[0].id == "doc1"
    assert results[0].rrf_score == round(2.0 / 61.0, 6)
    assert results[1].id == "doc2"
    assert results[1].rrf_score == round(0.5 / 61.0, 6)


def test_compute_rrf_deduplication_and_rank_accumulation():
    """Verify candidate appearing in both FTS and vector lists accumulates reciprocal scores."""
    shared = _make_candidate(doc_id="shared_item", title="Shared Item", rank=1)
    fts_only = _make_candidate(doc_id="fts_item", title="FTS Item", rank=2)
    vec_only = _make_candidate(doc_id="vec_item", title="Vec Item", rank=1)

    fts_list = [shared, fts_only]
    vec_list = [shared, vec_only]

    results = compute_rrf([fts_list, vec_list], rrf_k=60)
    assert len(results) == 3

    # shared item should be ranked first
    assert results[0].id == "shared_item"
    # shared item score = 1/61 (FTS rank 1) + 1/61 (Vec rank 1) = 2/61 ~ 0.032787
    assert results[0].rrf_score == round(2.0 / 61.0, 6)
    assert results[0].fts_rank == 1
    assert results[0].vector_rank == 1
    assert results[0].normalized_score == 1.0


def test_compute_rrf_duplicate_in_same_list_ignored():
    """Verify duplicate document ID in the same list is counted only at its highest rank."""
    doc_dup_1 = _make_candidate(doc_id="dup_doc", rank=1)
    doc_dup_2 = _make_candidate(doc_id="dup_doc", rank=2)

    results = compute_rrf([[doc_dup_1, doc_dup_2]], rrf_k=60)
    assert len(results) == 1
    # Should only get rank 1 score: 1/61
    assert results[0].rrf_score == round(1.0 / 61.0, 6)


def test_compute_rrf_score_normalization_max_one():
    """Verify normalized_score scales highest ranked item to 1.0 and others proportionally."""
    item1 = _make_candidate(doc_id="d1", rank=1)
    item2 = _make_candidate(doc_id="d2", rank=2)
    item3 = _make_candidate(doc_id="d3", rank=3)

    results = compute_rrf([[item1, item2, item3]], rrf_k=60)
    assert len(results) == 3
    assert results[0].normalized_score == 1.0
    assert 0.0 < results[1].normalized_score < 1.0
    assert 0.0 < results[2].normalized_score < results[1].normalized_score


def test_compute_rrf_empty_inputs_and_limit_bounds():
    """Verify behavior on empty lists, null lists, and limit <= 0."""
    assert compute_rrf([]) == []
    assert compute_rrf([[], []]) == []
    assert compute_rrf([[_make_candidate("d1")]], limit=0) == []
    assert compute_rrf([[_make_candidate("d1")]], limit=-5) == []


def test_compute_rrf_limit_truncation():
    """Verify limit truncates results to requested count."""
    items = [_make_candidate(f"doc_{i}", rank=i) for i in range(1, 20)]
    results = compute_rrf([items], limit=5)
    assert len(results) == 5
    assert results[0].id == "doc_1"
    assert results[4].id == "doc_5"


# ---------------------------------------------------------------------------
# 3. Helper Wrappers Tests
# ---------------------------------------------------------------------------


def test_compute_rrf_fusion_alias():
    """Verify compute_rrf_fusion functions as expected."""
    item = _make_candidate("doc_fusion", rank=1)
    res = compute_rrf_fusion([[item]], rrf_k=60, limit=10)
    assert len(res) == 1
    assert res[0].id == "doc_fusion"


def test_compute_rrf_score_wrapper():
    """Verify compute_rrf_score fuses explicit FTS and vector lists."""
    fts = [_make_candidate("fts_1", rank=1)]
    vec = [_make_candidate("vec_1", rank=1)]
    res = compute_rrf_score(fts, vec, k=60, fts_weight=1.0, vector_weight=1.0, limit=5)
    assert len(res) == 2


# ---------------------------------------------------------------------------
# 4. Context Attribution and Citation Formatting Tests
# ---------------------------------------------------------------------------


def test_format_context_attribution_empty():
    """Verify empty result list outputs descriptive fallback string."""
    assert format_context_attribution([]) == "No relevant internal knowledge found."


def test_format_context_attribution_structure():
    """Verify structured header attributes (source, version, scope, score) and markdown separator."""
    item1 = BlendedSearchResult(
        id="item-1",
        source_type="knowledge",
        title="Architecture",
        content="Clean Architecture Overview",
        rrf_score=0.032,
        normalized_score=0.95,
        version=2,
        workspace_id="ws-core",
        is_global=False,
    )
    item2 = BlendedSearchResult(
        id="item-2",
        source_type="document_chunk",
        title="README.md",
        content="Getting Started Guide",
        rrf_score=0.016,
        normalized_score=0.50,
        version=None,
        workspace_id="global",
        is_global=True,
    )

    formatted = format_context_attribution([item1, item2])
    assert "[1] Source: Architecture (knowledge, v2, Workspace: ws-core, Relevancy: 0.95)" in formatted
    assert "Clean Architecture Overview" in formatted
    assert "\n\n---\n\n" in formatted
    assert "[2] Source: README.md (document_chunk, latest, Global, Relevancy: 0.50)" in formatted
    assert "Getting Started Guide" in formatted


def test_format_context_attribution_truncation():
    """Verify long snippets are truncated cleanly with indicator."""
    long_text = "Python " * 200
    item = BlendedSearchResult(
        id="long-1",
        source_type="knowledge",
        title="Long Doc",
        content=long_text,
        rrf_score=0.03,
        normalized_score=1.0,
    )
    formatted = format_context_attribution([item], max_snippet_len=50)
    assert "... [truncated]" in formatted
    assert len(formatted.split("\n")[-1]) <= 70


def test_format_citation_compact():
    """Verify single-line compact citation generator."""
    item = BlendedSearchResult(
        id="doc-compact",
        source_type="knowledge",
        title="API Spec",
        content="Some content",
        rrf_score=0.03,
        normalized_score=0.99,
        version=3,
    )
    citation = format_citation(item)
    assert citation == "[Source: API Spec | ID: doc-compact | Revision: v3 | Score: 0.99]"
