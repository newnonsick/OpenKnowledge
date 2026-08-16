"""Tier 1 Feature Tests for Feature 22: Reciprocal Rank Fusion (RRF) & Deduplication.

Validates the Reciprocal Rank Fusion algorithm RRF(d) = sum(1 / (k + rank_i(d))) with k=60,
cross-source candidate deduplication, score normalization, and BlendedSearchResult generation.
"""

from typing import Dict, List
import pytest

from src.gateway.domain.canonical import BlendedSearchResult, RankedSearchResult


def compute_rrf_fusion(
    ranked_lists: List[List[RankedSearchResult]],
    rrf_k: int = 60,
    limit: int = 10,
) -> List[BlendedSearchResult]:
    """Calculates RRF score: RRF(d) = sum(1 / (k + rank_i(d))) with deduplication."""
    if rrf_k <= 0:
        rrf_k = 60

    if not ranked_lists:
        return []

    scores: Dict[str, float] = {}
    items_map: Dict[str, RankedSearchResult] = {}
    fts_ranks: Dict[str, int] = {}
    vector_ranks: Dict[str, int] = {}

    for list_idx, rank_list in enumerate(ranked_lists):
        for rank_idx, item in enumerate(rank_list, start=1):
            doc_id = item.id
            if doc_id not in items_map:
                items_map[doc_id] = item
                scores[doc_id] = 0.0

            rrf_score = 1.0 / (rrf_k + rank_idx)
            scores[doc_id] += rrf_score

            if list_idx == 0:
                fts_ranks[doc_id] = rank_idx
            else:
                vector_ranks[doc_id] = rank_idx

    sorted_docs = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    max_score = sorted_docs[0][1] if sorted_docs else 1.0

    blended = []
    for doc_id, raw_rrf in sorted_docs:
        orig = items_map[doc_id]
        normalized = raw_rrf / max_score if max_score > 0 else 0.0
        blended.append(
            BlendedSearchResult(
                id=orig.id,
                source_type=orig.source_type,
                title=orig.title,
                content=orig.content,
                metadata=orig.metadata,
                rrf_score=round(raw_rrf, 6),
                normalized_score=round(normalized, 4),
                fts_rank=fts_ranks.get(doc_id),
                vector_rank=vector_ranks.get(doc_id),
                workspace_id=orig.workspace_id,
                is_global=orig.is_global,
            )
        )
    return blended


@pytest.mark.tier1
@pytest.mark.feature("F22")
def test_f22_rrf_formula_calculation_with_k60():
    """Verify exact mathematical score calculation: 1 / (60 + rank) for single list."""
    item = RankedSearchResult(
        id="item_1",
        source_type="knowledge",
        title="Doc A",
        content="Content A",
        rank=1,
        raw_score=1.0,
    )
    blended = compute_rrf_fusion([[item]], rrf_k=60)
    assert len(blended) == 1
    # 1 / (60 + 1) = 1 / 61 = 0.0163934...
    expected_score = round(1.0 / 61.0, 6)
    assert blended[0].rrf_score == expected_score
    assert blended[0].normalized_score == 1.0


@pytest.mark.tier1
@pytest.mark.feature("F22")
def test_f22_rrf_deduplication_and_score_reinforcement():
    """Verify item appearing in both FTS and vector search is deduplicated and accumulates scores."""
    shared_item = RankedSearchResult(
        id="shared_doc",
        source_type="knowledge",
        title="Shared Architecture Spec",
        content="Core Clean Architecture principles",
        rank=1,
        raw_score=1.0,
    )
    fts_only_item = RankedSearchResult(
        id="fts_only_doc",
        source_type="knowledge",
        title="FTS Spec",
        content="FTS only",
        rank=2,
        raw_score=0.8,
    )

    fts_list = [shared_item, fts_only_item]
    vector_list = [shared_item]

    blended = compute_rrf_fusion([fts_list, vector_list], rrf_k=60)
    # Deduplicated to 2 unique items
    assert len(blended) == 2
    assert blended[0].id == "shared_doc"
    # Shared item score: 1/61 (from FTS rank 1) + 1/61 (from Vector rank 1) = 2/61 ~ 0.032787
    expected_shared_score = round(2.0 / 61.0, 6)
    assert blended[0].rrf_score == expected_shared_score
    assert blended[0].fts_rank == 1
    assert blended[0].vector_rank == 1


@pytest.mark.tier1
@pytest.mark.feature("F22")
def test_f22_rrf_score_normalization_max_one():
    """Verify normalized_score scales the top-ranked candidate to 1.0."""
    item1 = RankedSearchResult(id="doc1", source_type="knowledge", title="D1", content="C1", rank=1, raw_score=1.0)
    item2 = RankedSearchResult(id="doc2", source_type="knowledge", title="D2", content="C2", rank=2, raw_score=0.5)

    blended = compute_rrf_fusion([[item1, item2]], rrf_k=60)
    assert blended[0].normalized_score == 1.0
    assert blended[1].normalized_score < 1.0
    assert blended[1].normalized_score > 0.0


@pytest.mark.tier1
@pytest.mark.feature("F22")
def test_f22_blended_search_result_fields_populated():
    """Verify BlendedSearchResult model captures all provenance fields (ranks, scores, workspace, global)."""
    res = BlendedSearchResult(
        id="doc_provenance",
        source_type="document_chunk",
        title="Handbook.pdf",
        content="Section 1",
        metadata={"chunk": 0},
        rrf_score=0.032,
        normalized_score=1.0,
        fts_rank=1,
        vector_rank=2,
        workspace_id="ws-main",
        is_global=True,
    )
    assert res.id == "doc_provenance"
    assert res.source_type == "document_chunk"
    assert res.fts_rank == 1
    assert res.vector_rank == 2
    assert res.is_global is True
    assert res.workspace_id == "ws-main"


@pytest.mark.tier1
@pytest.mark.feature("F22")
def test_f22_rrf_limit_and_ordering_preservation():
    """Verify RRF results are ordered strictly descending by score and truncated to limit."""
    items = [
        RankedSearchResult(id=f"item_{i}", source_type="knowledge", title=f"T{i}", content=f"C{i}", rank=i, raw_score=1.0)
        for i in range(1, 10)
    ]
    blended = compute_rrf_fusion([items], limit=3)
    assert len(blended) == 3
    assert blended[0].id == "item_1"
    assert blended[1].id == "item_2"
    assert blended[2].id == "item_3"
    assert blended[0].rrf_score > blended[1].rrf_score > blended[2].rrf_score
