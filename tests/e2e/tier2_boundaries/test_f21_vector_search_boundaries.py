"""Tier 2 Boundary Tests for Feature 21: pgvector Semantic Search (Cosine <=>).

Tests boundary conditions, orthogonal vectors, zero vector query, extreme top_k limits, and empty index.
"""

import math
from typing import List, Tuple
import pytest


def cosine_distance(vec_a: List[float], vec_b: List[float]) -> float:
    """Computes cosine distance: 1 - cosine_similarity."""
    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0  # Maximum distance for zero vectors

    similarity = dot_product / (norm_a * norm_b)
    # Clamp float precision boundaries
    similarity = max(-1.0, min(1.0, similarity))
    return round(1.0 - similarity, 6)


def rank_by_vector_similarity(
    query_vec: List[float],
    candidates: List[Tuple[str, List[float]]],
    top_k: int = 5,
) -> List[Tuple[str, float]]:
    """Simulates pgvector `<=>` cosine distance ordering."""
    if top_k <= 0 or not candidates:
        return []

    scored = [
        (doc_id, cosine_distance(query_vec, doc_vec))
        for doc_id, doc_vec in candidates
    ]
    # Sort ascending by distance (closest first)
    scored.sort(key=lambda x: x[1])
    return scored[:top_k]


@pytest.mark.tier2
@pytest.mark.feature("F21")
def test_f21_boundary_identical_vectors_have_zero_cosine_distance():
    """Test boundary: identical unit vectors have distance = 0.0."""
    v1 = [0.6, 0.8, 0.0]
    v2 = [0.6, 0.8, 0.0]
    dist = cosine_distance(v1, v2)
    assert dist == 0.0


@pytest.mark.tier2
@pytest.mark.feature("F21")
def test_f21_boundary_orthogonal_vectors_have_unit_cosine_distance():
    """Test boundary: orthogonal perpendicular vectors have distance = 1.0."""
    v_x = [1.0, 0.0, 0.0]
    v_y = [0.0, 1.0, 0.0]
    dist = cosine_distance(v_x, v_y)
    assert dist == 1.0


@pytest.mark.tier2
@pytest.mark.feature("F21")
def test_f21_boundary_opposite_vectors_have_maximum_cosine_distance():
    """Test boundary: diametrically opposed vectors have distance = 2.0."""
    v_pos = [1.0, 0.0, 0.0]
    v_neg = [-1.0, 0.0, 0.0]
    dist = cosine_distance(v_pos, v_neg)
    assert dist == 2.0


@pytest.mark.tier2
@pytest.mark.feature("F21")
def test_f21_boundary_zero_vector_query_handling():
    """Test boundary: query vector with all zeroes returns max distance without division by zero."""
    zero_vec = [0.0, 0.0, 0.0]
    target = [0.5, 0.5, 0.707]
    dist = cosine_distance(zero_vec, target)
    assert dist == 1.0


@pytest.mark.tier2
@pytest.mark.feature("F21")
def test_f21_boundary_extreme_top_k_limits_and_empty_index():
    """Test boundary: top_k=0 returns empty list; top_k=1000 with 3 items returns 3 items; empty index returns []."""
    candidates = [
        ("doc1", [1.0, 0.0, 0.0]),
        ("doc2", [0.0, 1.0, 0.0]),
        ("doc3", [0.707, 0.707, 0.0]),
    ]
    query = [1.0, 0.0, 0.0]

    # top_k = 0
    res_k0 = rank_by_vector_similarity(query, candidates, top_k=0)
    assert res_k0 == []

    # top_k = 1000 on 3 items
    res_k1000 = rank_by_vector_similarity(query, candidates, top_k=1000)
    assert len(res_k1000) == 3
    assert res_k1000[0][0] == "doc1"
    assert res_k1000[0][1] == 0.0

    # empty candidate list
    res_empty = rank_by_vector_similarity(query, [], top_k=5)
    assert res_empty == []
