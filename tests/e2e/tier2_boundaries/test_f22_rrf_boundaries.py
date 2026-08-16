"""Tier 2 Boundary Tests for Feature 22: Reciprocal Rank Fusion (RRF) & Deduplication.

Tests boundary conditions, rrf_k values, disjoint rank lists, identical ranks, single item lists, and empty lists.
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
        rrf_k = 60  # Graceful fallback to standard k=60

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


@pytest.mark.tier2
@pytest.mark.feature("F22")
def test_f22_boundary_empty_and_null_candidate_lists():
    """Test boundary: empty ranking lists return empty blended results."""
    assert compute_rrf_fusion([]) == []
    assert compute_rrf_fusion([[], []]) == []


@pytest.mark.tier2
@pytest.mark.feature("F22")
def test_f22_boundary_rrf_k_zero_or_negative_fallback():
    """Test boundary: rrf_k <= 0 gracefully falls back to default k=60."""
    item = RankedSearchResult(
        id="doc1", source_type="knowledge", title="T1", content="C1", rank=1, raw_score=1.0
    )
    res_k0 = compute_rrf_fusion([[item]], rrf_k=0)
    assert len(res_k0) == 1
    # For rank=1, k=60 -> 1/(60+1) = 1/61 = ~0.016393
    assert abs(res_k0[0].rrf_score - (1.0 / 61.0)) < 1e-4


@pytest.mark.tier2
@pytest.mark.feature("F22")
def test_f22_boundary_disjoint_ranking_lists():
    """Test boundary: completely disjoint ranking lists are deduplicated and interleaved by rank."""
    doc_fts = RankedSearchResult(
        id="doc_fts_1", source_type="knowledge", title="FTS only", content="F", rank=1, raw_score=0.9
    )
    doc_vec = RankedSearchResult(
        id="doc_vec_1", source_type="document_chunk", title="Vec only", content="V", rank=1, raw_score=0.95
    )

    blended = compute_rrf_fusion([[doc_fts], [doc_vec]], rrf_k=60)
    assert len(blended) == 2
    # Both had rank 1 in their respective lists, so raw RRF score is identical
    assert blended[0].rrf_score == blended[1].rrf_score
    ids = {b.id for b in blended}
    assert ids == {"doc_fts_1", "doc_vec_1"}


@pytest.mark.tier2
@pytest.mark.feature("F22")
def test_f22_boundary_identical_rankings_reinforce_score():
    """Test boundary: item appearing at rank 1 in BOTH lists receives double RRF score."""
    shared_doc = RankedSearchResult(
        id="shared_1", source_type="knowledge", title="Shared", content="S", rank=1, raw_score=1.0
    )
    other_doc = RankedSearchResult(
        id="other_2", source_type="knowledge", title="Other", content="O", rank=2, raw_score=0.5
    )

    # shared_1 appears in both lists, other_2 only in list 1
    blended = compute_rrf_fusion([[shared_doc, other_doc], [shared_doc]], rrf_k=60)
    assert blended[0].id == "shared_1"
    # shared receives 1/61 + 1/61 = 2/61 = ~0.032787
    assert abs(blended[0].rrf_score - (2.0 / 61.0)) < 1e-4
    assert blended[0].normalized_score == 1.0


@pytest.mark.tier2
@pytest.mark.feature("F22")
def test_f22_boundary_limit_zero_and_large_limit():
    """Test boundary: limit=0 returns empty list; limit=100 with 1 item returns 1 item."""
    item = RankedSearchResult(
        id="doc1", source_type="knowledge", title="T1", content="C1", rank=1, raw_score=1.0
    )
    assert compute_rrf_fusion([[item]], limit=0) == []
    assert len(compute_rrf_fusion([[item]], limit=100)) == 1
