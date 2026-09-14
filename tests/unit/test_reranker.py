from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.gateway.application.services.authorized_retrieval_service import AuthorizedRetrievalService
from src.gateway.application.services.reranker import (
    exact_tag_matches,
    recency_score,
    rerank_hits,
    term_recall,
)
from src.gateway.application.services.runtime_settings_service import RetrievalRuntimeSettings
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.domain.retrieval import RetrievalCandidate, RetrievalHit


def _principal() -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read"}),
    )


def _candidate(
    title: str,
    content: str,
    *,
    tags: list[str] | None = None,
    version: int | None = None,
    updated_at: str | None = None,
) -> RetrievalCandidate:
    metadata: dict = {}
    if tags is not None:
        metadata["tags"] = list(tags)
    if updated_at is not None:
        metadata["updated_at"] = updated_at
    return RetrievalCandidate(
        unit_id=uuid4(),
        source_type="knowledge_revision",
        space_id="global",
        canonical_id=uuid4(),
        revision_id=uuid4(),
        title=title,
        content=content,
        embedding_generation_id=uuid4(),
        version=version,
        source_metadata=metadata,
    )


def _hit(rank: int, candidate: RetrievalCandidate, score: float) -> RetrievalHit:
    return RetrievalHit(
        rank=rank,
        candidate=candidate,
        rrf_score=score,
        rank_score=score,
        lexical_rank=rank,
        vector_rank=None,
        active_space_boost=0.0,
    )


def test_term_recall_counts_substring_terms_language_agnostic() -> None:
    assert term_recall("", "title", "content") == 0.0
    assert term_recall("backup runbook", "Backup runbook", "nightly backup runs") == 1.0
    assert term_recall("วาล์วน้ำสีฟ้า", "ขั้นตอนวาล์วน้ำ", "วาล์วน้ำสีฟ้าปิดตามเข็มนาฬิกา") == 1.0
    assert term_recall("backup missing", "Backup runbook", "nightly backup runs") == 0.5


def test_exact_tag_matches_require_full_term_equality() -> None:
    assert exact_tag_matches("ops oncall guide", ["ops", "operations"]) == 1
    assert exact_tag_matches("guide", []) == 0
    assert exact_tag_matches("", ["ops"]) == 0


def test_recency_score_prefers_recent_updates_and_tolerates_absent_signals() -> None:
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=1)).isoformat()
    stale = (now - timedelta(days=300)).isoformat()
    fresh_candidate = _candidate("fresh", "content", updated_at=recent)
    stale_candidate = _candidate("stale", "content", updated_at=stale)
    bare_candidate = _candidate("bare", "content")
    assert recency_score(fresh_candidate, now=now) > recency_score(stale_candidate, now=now)
    assert recency_score(bare_candidate, now=now) == 0.0
    assert recency_score(_candidate("bad", "content", updated_at="not-a-date"), now=now) == 0.0
    assert recency_score(_candidate("v", "content", version=5), now=now) > 0.0


def test_rerank_hits_is_deterministic_and_reorders_by_combined_signal() -> None:
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(days=400)).isoformat()
    distractor = _candidate("Unrelated scheduler", "rotate logs compact segments", updated_at=stale)
    relevant = _candidate(
        "Ingestion retry",
        "acquire lease heartbeat every interval retry with backoff on transient failure",
        tags=["ingestion", "retry"],
        updated_at=now.isoformat(),
    )
    hits = (
        _hit(1, distractor, 0.90),
        _hit(2, relevant, 0.40),
    )
    first = rerank_hits(
        hits,
        "ingestion retry backoff lease heartbeat",
        overlap_weight=0.25,
        tag_boost=0.15,
        recency_boost=0.10,
        now=now,
    )
    second = rerank_hits(
        hits,
        "ingestion retry backoff lease heartbeat",
        overlap_weight=0.25,
        tag_boost=0.15,
        recency_boost=0.10,
        now=now,
    )
    assert [hit.candidate.title for hit in first] == [hit.candidate.title for hit in second]
    assert first[0].candidate.title == "Ingestion retry"
    assert [hit.rank for hit in first] == [1, 2]
    assert all(hit.rank_score <= 1.0 for hit in first)


def test_rerank_hits_with_zero_weights_preserves_fusion_order() -> None:
    first_candidate = _candidate("alpha", "alpha content")
    second_candidate = _candidate("beta", "beta content")
    hits = (_hit(1, first_candidate, 0.8), _hit(2, second_candidate, 0.2))
    reranked = rerank_hits(
        hits,
        "unrelated query terms",
        overlap_weight=0.0,
        tag_boost=0.0,
        recency_boost=0.0,
    )
    assert [hit.candidate.title for hit in reranked] == ["alpha", "beta"]


class _FakeRepository:
    def __init__(self, candidates: list[RetrievalCandidate]) -> None:
        self.generation_id = uuid4()
        self.candidates = candidates

    async def active_generation(self, principal):
        return self.generation_id

    async def generation_coverage(self, principal, space_ids, generation_id):
        return 1.0

    async def lexical_search(self, principal, space_ids, query, generation_id, limit, minimum_score):
        return self.candidates[:limit]

    async def vector_search(
        self, principal, space_ids, query_vector, generation_id, limit, minimum_similarity, exact, hnsw_ef_search
    ):
        return []


async def _scope(member, requested):
    return ("global",)


class _FakeEmbeddingClient:
    async def embed_query(self, query):
        return [1.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_search_applies_reranker_only_when_enabled() -> None:
    distractor = _candidate("Unrelated scheduler", "rotate logs compact segments")
    relevant = _candidate("Ingestion retry", "acquire lease heartbeat retry with backoff")
    repository = _FakeRepository([distractor, relevant])
    service = AuthorizedRetrievalService(
        repository,
        _FakeEmbeddingClient(),
        scope_resolver=_scope,
    )
    baseline = await service.search(_principal(), "lease heartbeat backoff")
    assert [hit.candidate.title for hit in baseline.hits] == ["Unrelated scheduler", "Ingestion retry"]
    reranked = await service.search(_principal(), "lease heartbeat backoff", rerank_enabled=True)
    assert [hit.candidate.title for hit in reranked.hits] == ["Ingestion retry", "Unrelated scheduler"]


@pytest.mark.asyncio
async def test_search_rerank_settings_flow_through_runtime_defaults() -> None:
    distractor = _candidate("Unrelated scheduler", "rotate logs compact segments")
    relevant = _candidate("Ingestion retry", "acquire lease heartbeat retry with backoff")

    async def provider(principal):
        return RetrievalRuntimeSettings(rerank_enabled=True)

    repository = _FakeRepository([distractor, relevant])
    service = AuthorizedRetrievalService(
        repository,
        _FakeEmbeddingClient(),
        scope_resolver=_scope,
        runtime_settings_provider=provider,
    )
    response = await service.search(_principal(), "lease heartbeat backoff")
    assert [hit.candidate.title for hit in response.hits] == ["Ingestion retry", "Unrelated scheduler"]


@pytest.mark.asyncio
async def test_search_rejects_out_of_range_rerank_weights() -> None:
    service = AuthorizedRetrievalService(_FakeRepository([]), None, scope_resolver=_scope)
    with pytest.raises(ValueError):
        await service.search(_principal(), "query", rerank_enabled=True, rerank_tag_boost=1.5)
    with pytest.raises(ValueError):
        await service.search(_principal(), "query", rerank_overlap_weight=-0.1)
