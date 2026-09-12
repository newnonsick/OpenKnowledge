from uuid import uuid4

import pytest

from src.gateway.application.services.context_assembly_service import (
    AssembleContextQuery,
    ContextAssembler,
    query_snippet,
)
from src.gateway.application.services.runtime_settings_service import EffectiveRuntimePolicy
from src.gateway.application.use_cases.context import UseCaseContext
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.domain.retrieval import (
    RetrievalCandidate,
    RetrievalExplanation,
    RetrievalHealth,
    RetrievalHit,
    RetrievalResponse,
)


def _principal() -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read"}),
    )


def test_query_snippet_prefers_query_terms() -> None:
    content = "alpha " * 50 + "blue valve closes clockwise " + "omega " * 50
    snippet, truncated = query_snippet(content, "blue valve", max_chars=60)
    assert truncated is True
    assert "blue valve" in snippet


def test_query_snippet_short_content_not_truncated() -> None:
    snippet, truncated = query_snippet("short text", "short", max_chars=600)
    assert snippet == "short text"
    assert truncated is False


def _candidate(index: int, content: str, canonical=None) -> RetrievalHit:
    canonical_id = canonical or uuid4()
    return RetrievalHit(
        rank=index + 1,
        candidate=RetrievalCandidate(
            unit_id=uuid4(),
            source_type="knowledge_revision",
            space_id="global",
            canonical_id=canonical_id,
            revision_id=uuid4(),
            title=f"Note {index}",
            content=content,
            embedding_generation_id=uuid4(),
        ),
        rrf_score=1.0 / (index + 1),
        rank_score=0.9,
        lexical_rank=None,
        vector_rank=None,
        active_space_boost=0.0,
    )


def _service(hits):
    class _Stub:
        @staticmethod
        async def search(principal, query, **kwargs):
            return RetrievalResponse(
                query=query,
                hits=tuple(hits),
                health=RetrievalHealth(
                    semantic_status="active",
                    degraded_reasons=(),
                    embedding_generation_id=None,
                    embedding_coverage=None,
                ),
                explanation=RetrievalExplanation(
                    effective_space_ids=["global"],
                    lexical_candidates=(),
                    vector_candidates=(),
                    source_diversity_limit=0,
                    active_space_id=None,
                    abstained=False,
                ),
            )

    return _Stub


async def test_assemble_dedups_sources_and_enforces_budget() -> None:
    shared = uuid4()
    hits = [
        _candidate(0, "blue valve closes clockwise", canonical=shared),
        _candidate(1, "blue valve closes clockwise duplicate", canonical=shared),
        _candidate(2, "red valve opens counter-clockwise " * 40),
    ]
    assembler = ContextAssembler(service_factory=lambda: _service(hits))
    ctx = UseCaseContext(principal=_principal(), policy=EffectiveRuntimePolicy.default())

    package = await assembler.assemble(
        ctx,
        AssembleContextQuery(query="valve", max_sources=5, max_snippet_chars=60, max_total_chars=40),
    )

    assert [snippet.canonical_id for snippet in package.snippets] == [str(shared)]
    assert package.omitted_count == 2
    assert package.omitted_reason is not None
    assert package.total_chars <= 40
    assert package.abstained is False


async def test_assemble_abstains_without_evidence() -> None:
    assembler = ContextAssembler(service_factory=lambda: _service([]))
    ctx = UseCaseContext(principal=_principal(), policy=EffectiveRuntimePolicy.default())

    package = await assembler.assemble(ctx, AssembleContextQuery(query="nothing"))

    assert package.snippets == ()
    assert package.abstained is True


async def test_assemble_rejects_non_positive_budgets() -> None:
    assembler = ContextAssembler(service_factory=lambda: _service([]))
    ctx = UseCaseContext(principal=_principal(), policy=EffectiveRuntimePolicy.default())

    with pytest.raises(ValueError):
        await assembler.assemble(ctx, AssembleContextQuery(query="q", max_sources=0))


async def test_assemble_denies_without_knowledge_tools() -> None:
    assembler = ContextAssembler(service_factory=lambda: _service([]))
    policy = EffectiveRuntimePolicy.default()
    object.__setattr__(policy, "knowledge_tools_enabled", False)
    ctx = UseCaseContext(principal=_principal(), policy=policy)

    with pytest.raises(AuthorizationException):
        await assembler.assemble(ctx, AssembleContextQuery(query="q"))


async def test_assemble_marks_degraded_health() -> None:
    from src.gateway.domain.retrieval import RetrievalHealth as Health

    hits = [_candidate(0, "content here")]

    class _Degraded:
        @staticmethod
        async def search(principal, query, **kwargs):
            base = await _service(hits).search(principal, query, **kwargs)
            return RetrievalResponse(
                query=base.query,
                hits=base.hits,
                health=Health(
                    semantic_status="degraded",
                    degraded_reasons=("embedding_unavailable",),
                    embedding_generation_id=None,
                    embedding_coverage=None,
                ),
                explanation=base.explanation,
            )

    assembler = ContextAssembler(service_factory=_Degraded)
    ctx = UseCaseContext(principal=_principal(), policy=EffectiveRuntimePolicy.default())

    package = await assembler.assemble(ctx, AssembleContextQuery(query="content"))

    assert package.degraded is True
    assert len(package.snippets) == 1
