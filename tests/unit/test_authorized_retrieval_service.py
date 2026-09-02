from __future__ import annotations

from uuid import uuid4

import pytest

from src.gateway.application.services.authorized_retrieval_service import AuthorizedRetrievalService
from src.gateway.application.services.runtime_settings_service import RetrievalRuntimeSettings
from src.gateway.domain.exceptions import EmbeddingException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.domain.retrieval import RetrievalCandidate


class FakeRepository:
    def __init__(self) -> None:
        self.generation_id = uuid4()
        self.lexical: list[RetrievalCandidate] = []
        self.vector: list[RetrievalCandidate] = []
        self.lexical_calls = 0
        self.vector_calls = 0
        self.last_lexical_parameters = None

    async def active_generation(self, principal):
        return self.generation_id

    async def generation_coverage(self, principal, space_ids, generation_id):
        return 0.75

    async def lexical_search(self, principal, space_ids, query, generation_id, limit, minimum_score):
        self.lexical_calls += 1
        self.last_lexical_parameters = (limit, minimum_score)
        return self.lexical[:limit]

    async def vector_search(self, principal, space_ids, query_vector, generation_id, limit, minimum_similarity, exact, hnsw_ef_search):
        self.vector_calls += 1
        return self.vector[:limit]


class FakeEmbeddingClient:
    dimension = 3

    def __init__(self, result=None, failure=None) -> None:
        self.result = result or [1.0, 0.0, 0.0]
        self.failure = failure
        self.calls = 0

    async def embed_query(self, query):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result


def principal() -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read"}),
    )


def candidate(name: str, source: str = "knowledge_revision", space_id: str = "global") -> RetrievalCandidate:
    canonical_id = uuid4()
    return RetrievalCandidate(
        unit_id=uuid4(),
        source_type=source,
        space_id=space_id,
        canonical_id=canonical_id,
        revision_id=uuid4(),
        title=name,
        content=f"content {name}",
        embedding_generation_id=uuid4(),
        chunk_id=uuid4() if source == "document_chunk" else None,
    )


async def scope_resolver(member, requested):
    accessible = {"global", "family", "private"}
    if requested is not None:
        accessible &= requested
    return tuple(sorted(accessible))


@pytest.mark.asyncio
async def test_resolves_scope_once_fuses_branches_and_preserves_stable_citations():
    repository = FakeRepository()
    shared = candidate("shared", space_id="family")
    lexical_only = candidate("lexical")
    vector_only = candidate("vector", source="document_chunk")
    repository.lexical = [shared, lexical_only]
    repository.vector = [vector_only, shared]
    embedding = FakeEmbeddingClient()
    calls = 0

    async def tracked_scope(member, requested):
        nonlocal calls
        calls += 1
        return await scope_resolver(member, requested)

    service = AuthorizedRetrievalService(repository, embedding, scope_resolver=tracked_scope)
    response = await service.search(
        principal(),
        "family guide",
        requested_space_ids={"family", "missing"},
        active_space_id="family",
        limit=3,
    )

    assert calls == 1
    assert response.explanation.effective_space_ids == ("family",)
    assert response.hits[0].candidate.unit_id == shared.unit_id
    assert response.hits[0].candidate.citation_uri.startswith("openknowledge://spaces/family/knowledge/")
    assert response.hits[0].rank_score <= 1.0
    assert response.health.semantic_status == "active"
    assert response.health.embedding_coverage == 0.75


@pytest.mark.asyncio
async def test_embedding_outage_degrades_prefer_and_required_fails_closed():
    repository = FakeRepository()
    repository.lexical = [candidate("lexical")]
    embedding = FakeEmbeddingClient(failure=RuntimeError("secret provider detail"))
    service = AuthorizedRetrievalService(repository, embedding, scope_resolver=scope_resolver)

    degraded = await service.search(principal(), "query", semantic_policy="prefer")

    assert [hit.candidate.title for hit in degraded.hits] == ["lexical"]
    assert degraded.health.semantic_status == "degraded"
    assert degraded.health.degraded_reasons == ("embedding_provider_unavailable",)
    assert repository.vector_calls == 0
    with pytest.raises(EmbeddingException) as exc_info:
        await service.search(principal(), "query", semantic_policy="required")
    assert "secret provider detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_zero_weight_disables_branch_and_empty_scope_abstains_without_queries():
    repository = FakeRepository()
    embedding = FakeEmbeddingClient()
    service = AuthorizedRetrievalService(repository, embedding, scope_resolver=scope_resolver)

    lexical_only = await service.search(principal(), "query", vector_weight=0.0)
    empty = await service.search(
        principal(),
        "query",
        requested_space_ids={"missing"},
    )

    assert lexical_only.health.semantic_status == "disabled"
    assert embedding.calls == 0
    assert repository.vector_calls == 0
    assert empty.hits == ()
    assert empty.explanation.abstained is True
    assert repository.lexical_calls == 1


@pytest.mark.asyncio
async def test_source_diversity_limits_chunks_from_one_document():
    repository = FakeRepository()
    first = candidate("chunk-1", source="document_chunk")
    second = RetrievalCandidate(
        unit_id=uuid4(),
        source_type="document_chunk",
        space_id=first.space_id,
        canonical_id=first.canonical_id,
        revision_id=first.revision_id,
        title="chunk-2",
        content="content chunk-2",
        embedding_generation_id=first.embedding_generation_id,
        chunk_id=uuid4(),
    )
    third = candidate("other", source="knowledge_revision")
    repository.lexical = [first, second, third]
    service = AuthorizedRetrievalService(repository, None, scope_resolver=scope_resolver)

    response = await service.search(
        principal(),
        "query",
        semantic_policy="disabled",
        max_hits_per_source=1,
        limit=3,
    )

    assert [hit.candidate.title for hit in response.hits] == ["chunk-1", "other"]


@pytest.mark.asyncio
async def test_invalid_inputs_and_required_semantic_configuration_are_rejected():
    repository = FakeRepository()
    service = AuthorizedRetrievalService(repository, None, scope_resolver=scope_resolver)

    with pytest.raises(ValueError):
        await service.search(principal(), "", limit=3)
    with pytest.raises(ValueError):
        await service.search(principal(), "query", lexical_weight=-1.0)
    with pytest.raises(ValueError):
        await service.search(principal(), "query", semantic_policy="required", vector_weight=0.0)
    with pytest.raises(EmbeddingException):
        await service.search(principal(), "query", semantic_policy="required")


@pytest.mark.asyncio
async def test_active_safe_runtime_settings_supply_retrieval_defaults():
    repository = FakeRepository()
    repository.lexical = [candidate("first"), candidate("second")]

    async def runtime_settings(_principal):
        return RetrievalRuntimeSettings(
            limit=1,
            branch_limit=7,
            lexical_weight=1,
            vector_weight=0,
            minimum_lexical_score=0.2,
            semantic_policy="disabled",
        )

    service = AuthorizedRetrievalService(
        repository,
        None,
        scope_resolver=scope_resolver,
        runtime_settings_provider=runtime_settings,
    )
    response = await service.search(principal(), "query")

    assert [hit.candidate.title for hit in response.hits] == ["first"]
    assert repository.last_lexical_parameters == (7, 0.2)
    assert response.health.semantic_status == "disabled"
