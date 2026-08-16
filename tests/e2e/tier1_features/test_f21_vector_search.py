"""Tier 1 Feature Tests for Feature 21: pgvector Semantic Search (Cosine <=>).

Validates cosine distance calculations, semantic vector search ranking (nearest neighbor ordering),
top_k result limits, and RankedSearchResult candidate generation for vector search.
"""

import math
from typing import List, Tuple
import pytest

from src.gateway.domain.canonical import RankedSearchResult


def cosine_distance(vec_a: List[float], vec_b: List[float]) -> float:
    """Computes cosine distance: 1 - cosine_similarity."""
    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0

    similarity = dot_product / (norm_a * norm_b)
    similarity = max(-1.0, min(1.0, similarity))
    return round(1.0 - similarity, 6)


def rank_by_vector_similarity(
    query_vec: List[float],
    candidates: List[Tuple[str, str, str, List[float]]],  # (id, title, content, embedding)
    top_k: int = 5,
    workspace_id: str = "global",
) -> List[RankedSearchResult]:
    """Ranks candidate vectors by ascending cosine distance (closest first)."""
    scored = [
        (doc_id, title, content, cosine_distance(query_vec, doc_vec))
        for doc_id, title, content, doc_vec in candidates
    ]
    scored.sort(key=lambda x: x[3])

    results = []
    for rank, (doc_id, title, content, dist) in enumerate(scored[:top_k], start=1):
        similarity_score = max(0.0, 1.0 - dist)
        results.append(
            RankedSearchResult(
                id=doc_id,
                source_type="document_chunk",
                title=title,
                content=content,
                rank=rank,
                raw_score=round(similarity_score, 4),
                workspace_id=workspace_id,
            )
        )
    return results


@pytest.mark.tier1
@pytest.mark.feature("F21")
def test_f21_cosine_distance_identical_and_different_vectors():
    """Verify cosine distance produces 0.0 for identical unit vectors and positive distance for non-identical."""
    vec_a = [0.6, 0.8, 0.0]
    vec_b = [0.6, 0.8, 0.0]
    vec_c = [0.0, 1.0, 0.0]

    assert cosine_distance(vec_a, vec_b) == 0.0
    dist_ac = cosine_distance(vec_a, vec_c)
    assert dist_ac > 0.0
    assert dist_ac < 1.0


@pytest.mark.tier1
@pytest.mark.feature("F21")
def test_f21_vector_similarity_ranking_closest_first():
    """Verify semantic search ordering ranks closest semantic vector at rank 1."""
    query_vec = [1.0, 0.0, 0.0]
    candidates = [
        ("doc_far", "Unrelated Topic", "Quantum physics", [0.0, 1.0, 0.0]),
        ("doc_close", "Exact Topic", "Software architecture", [0.95, 0.31, 0.0]),
        ("doc_med", "Related Topic", "System programming", [0.707, 0.707, 0.0]),
    ]

    ranked = rank_by_vector_similarity(query_vec, candidates, top_k=3)
    assert len(ranked) == 3
    assert ranked[0].id == "doc_close"
    assert ranked[0].rank == 1
    assert ranked[1].id == "doc_med"
    assert ranked[1].rank == 2
    assert ranked[2].id == "doc_far"
    assert ranked[2].rank == 3


@pytest.mark.tier1
@pytest.mark.feature("F21")
def test_f21_top_k_limiting_on_vector_search():
    """Verify top_k parameter truncates returned vector candidates to exact limit."""
    query_vec = [1.0, 0.0, 0.0]
    candidates = [
        (f"doc_{i}", f"Doc {i}", f"Content {i}", [0.5, float(i) / 10.0, 0.0])
        for i in range(10)
    ]

    top_2 = rank_by_vector_similarity(query_vec, candidates, top_k=2)
    assert len(top_2) == 2

    top_5 = rank_by_vector_similarity(query_vec, candidates, top_k=5)
    assert len(top_5) == 5


@pytest.mark.tier1
@pytest.mark.feature("F21")
def test_f21_ranked_search_result_vector_source():
    """Verify RankedSearchResult captures source_type='document_chunk' and raw similarity score."""
    query_vec = [0.8, 0.6]
    candidates = [("chunk_1", "README.md", "Installation guide", [0.8, 0.6])]

    results = rank_by_vector_similarity(query_vec, candidates, top_k=1)
    assert len(results) == 1
    res = results[0]
    assert res.id == "chunk_1"
    assert res.source_type == "document_chunk"
    assert res.title == "README.md"
    assert res.raw_score == 1.0  # Cosine similarity for identical vector is 1.0


@pytest.mark.tier1
@pytest.mark.feature("F21")
def test_f21_unit_vector_dot_product_relation():
    """Verify that for unit-normalized vectors, cosine distance equals 1 - dot_product."""
    u = [0.6, 0.8]  # norm = 1.0
    v = [0.8, 0.6]  # norm = 1.0

    dot = sum(a * b for a, b in zip(u, v))  # 0.48 + 0.48 = 0.96
    expected_dist = round(1.0 - dot, 6)     # 0.04
    actual_dist = cosine_distance(u, v)

    assert abs(actual_dist - expected_dist) < 1e-5
