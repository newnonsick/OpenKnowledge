from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Sequence

from src.gateway.domain.retrieval import RetrievalHit


_TOKEN_PATTERN = re.compile(r"[0-9a-z\u0e00-\u0e7f]+", re.IGNORECASE)


def tokenize_terms(value: str) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _TOKEN_PATTERN.finditer(value.lower()):
        term = match.group(0)
        if term not in seen:
            seen.add(term)
            ordered.append(term)
    return tuple(ordered)


def term_recall(query: str, title: str, content: str) -> float:
    terms = tokenize_terms(query)
    if not terms:
        return 0.0
    haystack = f"{title}\n{content}".lower()
    if not haystack.strip():
        return 0.0
    matched = sum(1 for term in terms if term in haystack)
    return matched / len(terms)


def candidate_tags(candidate) -> tuple[str, ...]:
    metadata = candidate.source_metadata or {}
    raw_tags = metadata.get("tags")
    if not isinstance(raw_tags, list):
        return ()
    return tuple(str(tag).strip().lower() for tag in raw_tags if str(tag).strip())


def exact_tag_matches(query: str, tags: Sequence[str]) -> int:
    terms = set(tokenize_terms(query))
    if not terms:
        return 0
    return sum(1 for tag in tags if tag.lower() in terms)


def recency_score(candidate, *, now: datetime | None = None) -> float:
    metadata = candidate.source_metadata or {}
    raw_updated = metadata.get("updated_at")
    if isinstance(raw_updated, str) and raw_updated.strip():
        try:
            updated = datetime.fromisoformat(raw_updated.strip())
        except ValueError:
            updated = None
        if updated is not None:
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            reference = now or datetime.now(timezone.utc)
            age_days = max(0.0, (reference - updated).total_seconds() / 86400.0)
            return 1.0 / (1.0 + age_days / 30.0)
    version = candidate.version
    if isinstance(version, int) and version > 1:
        return min(1.0, (version - 1) / 9.0)
    return 0.0


def rerank_score(
    hit: RetrievalHit,
    query: str,
    *,
    overlap_weight: float,
    tag_boost: float,
    recency_boost: float,
    now: datetime | None = None,
) -> float:
    overlap = term_recall(query, hit.candidate.title, hit.candidate.content)
    tags = candidate_tags(hit.candidate)
    tag_matches = exact_tag_matches(query, tags)
    freshness = recency_score(hit.candidate, now=now)
    return (
        hit.rank_score
        + overlap_weight * overlap
        + tag_boost * min(tag_matches, 3)
        + recency_boost * freshness
    )


def rerank_hits(
    hits: Sequence[RetrievalHit],
    query: str,
    *,
    overlap_weight: float,
    tag_boost: float,
    recency_boost: float,
    now: datetime | None = None,
) -> tuple[RetrievalHit, ...]:
    scored = []
    for hit in hits:
        overlap = term_recall(query, hit.candidate.title, hit.candidate.content)
        tags = candidate_tags(hit.candidate)
        tag_matches = exact_tag_matches(query, tags)
        freshness = recency_score(hit.candidate, now=now)
        final = min(
            1.0,
            hit.rank_score
            + overlap_weight * overlap
            + tag_boost * min(tag_matches, 3)
            + recency_boost * freshness,
        )
        scored.append(
            (final, overlap, freshness, str(hit.candidate.canonical_id), str(hit.candidate.unit_id), hit)
        )
    scored.sort(key=lambda entry: (-entry[0], -entry[1], -entry[2], entry[3], entry[4]))
    reranked: list[RetrievalHit] = []
    for rank, (final_score, _, _, _, _, hit) in enumerate(scored, start=1):
        reranked.append(
            RetrievalHit(
                rank=rank,
                candidate=hit.candidate,
                rrf_score=hit.rrf_score,
                rank_score=final_score,
                lexical_rank=hit.lexical_rank,
                vector_rank=hit.vector_rank,
                active_space_boost=hit.active_space_boost,
            )
        )
    return tuple(reranked)
