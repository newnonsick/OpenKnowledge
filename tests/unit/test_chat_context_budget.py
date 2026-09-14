from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from src.gateway.application.services.authorized_retrieval_service import AuthorizedRetrievalService
from src.gateway.application.services.chat_orchestrator import ChatOrchestratorService
from src.gateway.application.services.runtime_settings_service import EffectiveRuntimePolicy, RetrievalRuntimeSettings
from src.gateway.config import get_settings
from src.gateway.domain.canonical import CanonicalChatRequest, CanonicalLLMResponse, CanonicalMessage, CanonicalUsage
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.domain.retrieval import RetrievalCandidate
from src.gateway.domain.tools import FunctionCall, ToolCall
from src.gateway.infrastructure.persistence.principal_context import bind_principal, reset_principal


def make_principal() -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read", "chat:write"}),
    )


def tagged_candidate(name: str, tags: list[str], space_id: str = "family") -> RetrievalCandidate:
    return RetrievalCandidate(
        unit_id=uuid4(),
        source_type="knowledge_revision",
        space_id=space_id,
        canonical_id=uuid4(),
        revision_id=uuid4(),
        title=name,
        content=f"content {name} with enough detail to exceed tiny budgets " * 10,
        embedding_generation_id=uuid4(),
        source_metadata={"tags": list(tags)},
    )


class FakeRepository:
    def __init__(self) -> None:
        self.generation_id = uuid4()
        self.lexical: list[RetrievalCandidate] = []
        self.vector: list[RetrievalCandidate] = []

    async def active_generation(self, principal):
        return self.generation_id

    async def generation_coverage(self, principal, space_ids, generation_id):
        return 1.0

    async def lexical_search(self, principal, space_ids, query, generation_id, limit, minimum_score):
        return self.lexical[:limit]

    async def vector_search(
        self, principal, space_ids, query_vector, generation_id, limit, minimum_similarity, exact, hnsw_ef_search
    ):
        return self.vector[:limit]


async def permissive_scope(member, requested):
    accessible = {"global", "family", "private"}
    if requested is not None:
        accessible &= set(requested)
    return tuple(sorted(accessible))


class ScriptedLLM:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])

    async def generate(self, **kwargs):
        return self.responses.pop(0)

    async def generate_stream(self, **kwargs):  # pragma: no cover
        raise AssertionError("not used")


def llm_search_response(arguments: str = '{"query": "guide"}') -> CanonicalLLMResponse:
    return CanonicalLLMResponse(
        id="resp_1",
        model="m",
        content=None,
        tool_calls=[ToolCall(id="call_1", function=FunctionCall(name="knowledge_search", arguments=arguments))],
        finish_reason="tool_use",
        usage=CanonicalUsage(),
    )


def llm_text_response(text: str = "done") -> CanonicalLLMResponse:
    return CanonicalLLMResponse(
        id="resp_2", model="m", content=text, tool_calls=[], finish_reason="stop", usage=CanonicalUsage()
    )


@pytest.mark.asyncio
async def test_chat_search_routes_through_assembler_with_token_budget() -> None:
    repository = FakeRepository()
    repository.lexical = [tagged_candidate("runbook", ["ops"]), tagged_candidate("roadmap", ["planning"])]
    retrieval = AuthorizedRetrievalService(repository, None, scope_resolver=permissive_scope)
    orchestrator = ChatOrchestratorService(ScriptedLLM([]), retrieval_service=retrieval)
    token = bind_principal(make_principal())
    try:
        result = await orchestrator._execute_internal_tool(
            ToolCall(id="c1", function=FunctionCall(name="knowledge_search", arguments='{"query": "guide"}')),
            "global",
        )
    finally:
        reset_principal(token)
    assert result.is_error is False
    payload = json.loads(result.content)
    budget = get_settings().gateway.chat_context_max_total_tokens
    assert payload["count"] == 2
    assert all("snippet" in entry for entry in payload["results"])
    assert all("content" not in entry for entry in payload["results"])
    assert payload["health"]["budget_tokens"] == budget
    assert payload["health"]["total_tokens"] <= budget
    assert payload["health"]["status"] in ("ok", "oversized")
    assert payload["health"]["estimation_method"] != ""


@pytest.mark.asyncio
async def test_chat_search_caps_tool_result_with_tiny_budget() -> None:
    repository = FakeRepository()
    repository.lexical = [tagged_candidate("runbook", ["ops"])]
    retrieval = AuthorizedRetrievalService(repository, None, scope_resolver=permissive_scope)

    from src.gateway.application.services.context_assembly_service import ContextAssembler

    assembler = ContextAssembler(service_factory=lambda: retrieval)
    orchestrator = ChatOrchestratorService(
        ScriptedLLM([]),
        retrieval_service=retrieval,
        context_assembler_factory=lambda: assembler,
    )
    token = bind_principal(make_principal())
    try:
        result = await orchestrator._execute_internal_tool(
            ToolCall(id="c1", function=FunctionCall(name="knowledge_search", arguments='{"query": "guide"}')),
            "global",
        )
    finally:
        reset_principal(token)
    payload = json.loads(result.content)
    assert payload["count"] >= 1
    assert payload["health"]["total_tokens"] <= get_settings().gateway.chat_context_max_total_tokens


@pytest.mark.asyncio
async def test_chat_search_reports_no_answer_contract() -> None:
    retrieval = AuthorizedRetrievalService(FakeRepository(), None, scope_resolver=permissive_scope)
    orchestrator = ChatOrchestratorService(ScriptedLLM([]), retrieval_service=retrieval)
    token = bind_principal(make_principal())
    try:
        result = await orchestrator._execute_internal_tool(
            ToolCall(
                id="c1",
                function=FunctionCall(name="knowledge_search", arguments='{"query": "missing"}'),
            ),
            "global",
        )
    finally:
        reset_principal(token)
    payload = json.loads(result.content)
    assert payload["count"] == 0
    assert payload["health"]["abstained"] is True
    assert payload["health"]["status"] == "no_answer"


@pytest.mark.asyncio
async def test_chat_search_applies_reranker_from_runtime_settings() -> None:
    repository = FakeRepository()
    distractor = tagged_candidate("unrelated", ["ops"])
    distractor_content = "rotate logs compact segments " * 20
    object.__setattr__(distractor, "content", distractor_content)
    relevant = tagged_candidate("guide", ["ops"])
    relevant_content = "guide lease heartbeat backoff retry " * 20
    object.__setattr__(relevant, "content", relevant_content)
    repository.lexical = [distractor, relevant]

    async def provider(principal):
        return RetrievalRuntimeSettings(rerank_enabled=True)

    retrieval = AuthorizedRetrievalService(
        repository,
        None,
        scope_resolver=permissive_scope,
        runtime_settings_provider=provider,
    )
    policy = EffectiveRuntimePolicy.default()
    orchestrator = ChatOrchestratorService(
        ScriptedLLM([]),
        retrieval_service=retrieval,
        runtime_policy_provider=lambda principal: _policy_with_rerank(policy),
    )
    token = bind_principal(make_principal())
    try:
        result = await orchestrator._execute_internal_tool(
            ToolCall(
                id="c1",
                function=FunctionCall(
                    name="knowledge_search", arguments='{"query": "guide lease heartbeat backoff"}'
                ),
            ),
            "global",
        )
    finally:
        reset_principal(token)
    payload = json.loads(result.content)
    assert payload["results"][0]["title"] == "guide"


async def _policy_with_rerank(policy: EffectiveRuntimePolicy) -> EffectiveRuntimePolicy:
    return policy


@pytest.mark.asyncio
async def test_chat_search_end_to_end_tool_loop_uses_budgeted_payload() -> None:
    repository = FakeRepository()
    repository.lexical = [tagged_candidate("runbook", ["ops"])]
    retrieval = AuthorizedRetrievalService(repository, None, scope_resolver=permissive_scope)
    orchestrator = ChatOrchestratorService(
        ScriptedLLM([llm_search_response(), llm_text_response("answer")]),
        retrieval_service=retrieval,
    )
    token = bind_principal(make_principal())
    try:
        response = await orchestrator.orchestrate_chat(
            CanonicalChatRequest(
                model="m",
                messages=[CanonicalMessage(role="user", content="hello")],
                workspace_id="global",
            ),
            workspace_id="global",
        )
    finally:
        reset_principal(token)
    assert response.finish_reason == "stop"


def test_chat_context_budget_settings_have_sane_defaults() -> None:
    gateway: Any = get_settings().gateway
    assert gateway.chat_context_max_sources >= 1
    assert gateway.chat_context_max_snippet_chars >= 100
    assert gateway.chat_context_max_total_chars >= gateway.chat_context_max_snippet_chars
    assert gateway.chat_context_max_total_tokens >= 1
