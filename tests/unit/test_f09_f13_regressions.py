from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pytest

from src.gateway.application.services.action_review_service import REDACTED, build_action_review
from src.gateway.application.services.authorized_retrieval_service import AuthorizedRetrievalService
from src.gateway.application.services.chat_orchestrator import ChatOrchestratorService
from src.gateway.application.services.idempotency_service import IdempotencyService
from src.gateway.application.services.runtime_settings_service import EffectiveRuntimePolicy, RetrievalRuntimeSettings
from src.gateway.domain.authorization import narrow_requested_spaces, resolve_request_space_scope
from src.gateway.domain.canonical import (
    CanonicalChatRequest,
    CanonicalLLMResponse,
    CanonicalMessage,
    CanonicalTextBlock,
    CanonicalUsage,
)
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.domain.retrieval import RetrievalCandidate
from src.gateway.domain.tools import FunctionCall, ToolCall
from src.gateway.infrastructure.persistence.principal_context import bind_principal, reset_principal
from src.gateway.presentation.converters.openai_converter import openai_request_to_canonical
from src.gateway.presentation.schemas.openai_schemas import OpenAIChatCompletionRequest


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
        content=f"content {name}",
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


class FakeEmbeddingClient:
    async def embed_query(self, query):
        return [1.0, 0.0, 0.0]


async def permissive_scope(member, requested):
    accessible = {"global", "family", "private"}
    if requested is not None:
        accessible &= set(requested)
    return tuple(sorted(accessible))


class ScriptedLLM:
    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)

    async def generate_stream(self, **kwargs):  # pragma: no cover
        raise AssertionError("not used")


def llm_search_response(call_id: str = "call_1", arguments: str = '{"query": "guide"}') -> CanonicalLLMResponse:
    return CanonicalLLMResponse(
        id="resp_1",
        model="m",
        content=None,
        tool_calls=[ToolCall(id=call_id, function=FunctionCall(name="knowledge_search", arguments=arguments))],
        finish_reason="tool_use",
        usage=CanonicalUsage(),
    )


def llm_text_response(text: str = "done") -> CanonicalLLMResponse:
    return CanonicalLLMResponse(
        id="resp_2", model="m", content=text, tool_calls=[], finish_reason="stop", usage=CanonicalUsage()
    )


def chat_request(workspace_id: str = "global") -> CanonicalChatRequest:
    return CanonicalChatRequest(
        model="m",
        messages=[CanonicalMessage(role="user", content=[CanonicalTextBlock(text="hello")])],
        workspace_id=workspace_id,
    )


def disabled_policy(**overrides) -> EffectiveRuntimePolicy:
    base = {
        "revision": 3,
        "knowledge_tools_enabled": True,
        "mutation_tools_enabled": True,
        "destructive_tools_require_confirmation": True,
        "semantic_retrieval_enabled": True,
        "retrieval_explanations_enabled": True,
        "retrieval": RetrievalRuntimeSettings(),
    }
    base.update(overrides)
    return EffectiveRuntimePolicy(**base)


# ---------------------------------------------------------------------------
# F12: n != 1 is rejected before upstream and never forwarded
# ---------------------------------------------------------------------------


def openai_request(n: Any) -> OpenAIChatCompletionRequest:
    return OpenAIChatCompletionRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        n=n,
    )


def test_f12_converter_never_forwards_n_to_upstream():
    canonical = openai_request_to_canonical(openai_request(2))
    assert "n" not in canonical.extra_params
    canonical_default = openai_request_to_canonical(openai_request(1))
    assert "n" not in canonical_default.extra_params


@pytest.mark.asyncio
async def test_f12_chat_completion_rejects_n_not_equal_1_without_calling_orchestrator():
    from src.gateway.main import create_app
    from src.gateway.presentation.routers import chat_completions
    from src.gateway.config import Settings
    import httpx

    called = False
    ok_calls = 0

    class SpyOrchestrator:
        async def orchestrate_chat(self, request, workspace_id="global"):
            nonlocal called, ok_calls
            called = True
            n_value = (request.extra_params or {}).get("n")
            assert n_value is None, "n must never reach the orchestrator or upstream"
            ok_calls += 1
            from src.gateway.domain.canonical import CanonicalChatResponse, CanonicalTextBlock

            return CanonicalChatResponse(
                id="ok", model=request.model, content=[CanonicalTextBlock(text="hi")]
            )

        async def orchestrate_chat_stream(self, request, workspace_id="global"):  # pragma: no cover
            raise AssertionError("must not stream")

    app = create_app(
        Settings(
            gateway={
                "environment": "test",
                "api_keys": ["f12-key"],
                "legacy_api_keys_enabled": True,
            }
        )
    )
    app.dependency_overrides[chat_completions.get_chat_orchestrator] = lambda: SpyOrchestrator()
    headers = {"Authorization": "Bearer f12-key"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        body = {"model": "default", "messages": [{"role": "user", "content": "hi"}], "n": 2}
        json_resp = await client.post("/v1/chat/completions", headers=headers, json=body)
        assert json_resp.status_code == 400
        assert json_resp.json()["error"]["param"] == "n"
        stream_resp = await client.post(
            "/v1/chat/completions", headers=headers, json={**body, "stream": True}
        )
        assert stream_resp.status_code == 400
        assert stream_resp.json()["error"]["param"] == "n"
        ok_resp = await client.post(
            "/v1/chat/completions", headers=headers, json={**body, "n": 1}
        )
        assert ok_resp.status_code == 200
        assert ok_resp.json()["choices"][0]["message"]["content"] == "hi"
    assert called is True
    assert ok_calls == 1


# ---------------------------------------------------------------------------
# F11: tag filter applies to both branches pre-fusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f11_tag_filter_restricts_both_branches_pre_fusion():
    repository = FakeRepository()
    repository.lexical = [
        tagged_candidate("runbook", ["ops"]),
        tagged_candidate("roadmap", ["planning"]),
    ]
    repository.vector = [
        tagged_candidate("oncall", ["ops"]),
        tagged_candidate("vision", ["planning"]),
    ]
    service = AuthorizedRetrievalService(
        repository, FakeEmbeddingClient(), scope_resolver=permissive_scope
    )
    response = await service.search(make_principal(), "guide", tags=["ops"])
    titles = sorted(hit.candidate.title for hit in response.hits)
    assert titles == ["oncall", "runbook"]
    for hit in response.hits:
        assert hit.lexical_rank is not None or hit.vector_rank is not None


@pytest.mark.asyncio
async def test_f11_unknown_and_empty_tags():
    repository = FakeRepository()
    repository.lexical = [tagged_candidate("runbook", ["ops"])]
    service = AuthorizedRetrievalService(
        repository, FakeEmbeddingClient(), scope_resolver=permissive_scope
    )
    unknown = await service.search(make_principal(), "guide", tags=["nope"])
    assert unknown.hits == ()
    assert unknown.explanation.abstained is True
    unfiltered = await service.search(make_principal(), "guide", tags=[])
    assert [hit.candidate.title for hit in unfiltered.hits] == ["runbook"]


@pytest.mark.asyncio
async def test_f11_chat_tool_passes_tags_to_retrieval():
    repository = FakeRepository()
    repository.lexical = [tagged_candidate("runbook", ["ops"]), tagged_candidate("roadmap", ["planning"])]
    retrieval = AuthorizedRetrievalService(repository, None, scope_resolver=permissive_scope)
    orchestrator = ChatOrchestratorService(ScriptedLLM([llm_text_response()]), retrieval_service=retrieval)
    token = bind_principal(make_principal())
    try:
        result = await orchestrator._execute_internal_tool(
            ToolCall(
                id="c1",
                function=FunctionCall(name="knowledge_search", arguments='{"query": "x", "tags": ["ops"]}'),
            ),
            "global",
        )
    finally:
        reset_principal(token)
    payload = json.loads(result.content)
    assert payload["count"] == 1


# ---------------------------------------------------------------------------
# F13: hard request space scope, tool args narrow only
# ---------------------------------------------------------------------------


def test_f13_scope_helpers():
    assert resolve_request_space_scope("global", "global") is None
    assert resolve_request_space_scope(None, "global") is None
    assert resolve_request_space_scope("  ", "global") is None
    assert resolve_request_space_scope("family", "global") == frozenset({"family"})
    assert narrow_requested_spaces(None, None) is None
    assert narrow_requested_spaces(frozenset({"family"}), None) == {"family"}
    assert narrow_requested_spaces(None, {"family", "private"}) == {"family", "private"}
    assert narrow_requested_spaces(frozenset({"family"}), {"family", "private"}) == {"family"}
    assert narrow_requested_spaces(frozenset({"family"}), {"private"}) == set()


@pytest.mark.asyncio
async def test_f13_tool_workspace_cannot_widen_request_scope():
    seen: dict = {}

    async def capturing_scope(member, requested):
        seen["requested"] = requested
        return await permissive_scope(member, requested)

    repository = FakeRepository()
    repository.lexical = [tagged_candidate("runbook", ["ops"], space_id="family")]
    retrieval = AuthorizedRetrievalService(repository, None, scope_resolver=capturing_scope)
    orchestrator = ChatOrchestratorService(ScriptedLLM([]), retrieval_service=retrieval)
    token = bind_principal(make_principal())
    try:
        result = await orchestrator._execute_internal_tool(
            ToolCall(
                id="c1",
                function=FunctionCall(name="knowledge_search", arguments='{"query": "x", "workspace_id": "private"}'),
            ),
            "family",
            frozenset({"family"}),
        )
    finally:
        reset_principal(token)
    assert seen["requested"] == set()
    assert json.loads(result.content)["count"] == 0


@pytest.mark.asyncio
async def test_f13_global_request_searches_every_accessible_space():
    seen: dict = {}

    async def capturing_scope(member, requested):
        seen["requested"] = requested
        return await permissive_scope(member, requested)

    repository = FakeRepository()
    retrieval = AuthorizedRetrievalService(repository, None, scope_resolver=capturing_scope)
    orchestrator = ChatOrchestratorService(
        ScriptedLLM([llm_search_response(), llm_text_response()]),
        retrieval_service=retrieval,
    )
    token = bind_principal(make_principal())
    try:
        await orchestrator.orchestrate_chat(chat_request(), workspace_id="global")
    finally:
        reset_principal(token)
    assert seen["requested"] is None


# ---------------------------------------------------------------------------
# F09: effective policy snapshot enforced on chat surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f09_knowledge_tools_disabled_hides_discovery_and_blocks_execution():
    repository = FakeRepository()
    retrieval = AuthorizedRetrievalService(repository, None, scope_resolver=permissive_scope)

    async def policy_provider(principal):
        return disabled_policy(knowledge_tools_enabled=False)

    orchestrator = ChatOrchestratorService(
        ScriptedLLM([llm_search_response(), llm_text_response()]),
        retrieval_service=retrieval,
        runtime_policy_provider=policy_provider,
    )
    combined, upstream = orchestrator._prepare_tools([], disabled_policy(knowledge_tools_enabled=False))
    assert combined == []
    assert upstream is None
    token = bind_principal(make_principal())
    try:
        result = await orchestrator._execute_internal_tool(
            ToolCall(id="c1", function=FunctionCall(name="knowledge_search", arguments='{"query": "x"}')),
            "global",
            None,
            disabled_policy(knowledge_tools_enabled=False),
        )
    finally:
        reset_principal(token)
    assert result.is_error is True
    assert "disabled" in json.loads(result.content)["error"]


@pytest.mark.asyncio
async def test_f09_semantic_disabled_forces_disabled_policy_and_redacts_explanations():
    repository = FakeRepository()
    repository.lexical = [tagged_candidate("runbook", ["ops"])]
    retrieval = AuthorizedRetrievalService(repository, None, scope_resolver=permissive_scope)

    async def policy_provider(principal):
        return disabled_policy(semantic_retrieval_enabled=False, retrieval_explanations_enabled=False)

    orchestrator = ChatOrchestratorService(
        ScriptedLLM(
            [
                llm_search_response(),
                llm_text_response(),
            ]
        ),
        retrieval_service=retrieval,
        runtime_policy_provider=policy_provider,
    )
    token = bind_principal(make_principal())
    try:
        await orchestrator.orchestrate_chat(chat_request(), workspace_id="global")
    finally:
        reset_principal(token)
    assert repository.lexical is not None


@pytest.mark.asyncio
async def test_f09_in_flight_request_keeps_entry_snapshot():
    repository = FakeRepository()
    repository.lexical = [tagged_candidate("runbook", ["ops"])]
    retrieval = AuthorizedRetrievalService(repository, None, scope_resolver=permissive_scope)
    calls = 0

    async def flipping_provider(principal):
        nonlocal calls
        calls += 1
        if calls == 1:
            return disabled_policy()
        return disabled_policy(knowledge_tools_enabled=False)

    orchestrator = ChatOrchestratorService(
        ScriptedLLM([llm_search_response(), llm_text_response()]),
        retrieval_service=retrieval,
        runtime_policy_provider=flipping_provider,
    )
    token = bind_principal(make_principal())
    try:
        response = await orchestrator.orchestrate_chat(chat_request(), workspace_id="global")
    finally:
        reset_principal(token)
    assert calls == 1
    assert response.finish_reason == "stop"


def test_f09_policy_snapshot_helpers():
    assert EffectiveRuntimePolicy.default().knowledge_tools_enabled is True
    revision_values = {
        "retrieval": {},
        "chunking": {},
        "tools": {"knowledge_tools_enabled": False},
        "features": {"semantic_retrieval_enabled": False},
    }
    from src.gateway.application.services.runtime_settings_service import RuntimeSettingsValues

    values = RuntimeSettingsValues.model_validate(revision_values)
    policy = EffectiveRuntimePolicy.from_revision(
        __import__(
            "src.gateway.application.services.runtime_settings_service", fromlist=["RuntimeSettingsRevision"]
        ).RuntimeSettingsRevision(
            id=None, revision=4, base_revision=3, state="active", values=values,
            created_at=None, activated_at=None,
        )
    )
    assert policy.revision == 4
    assert policy.knowledge_tools_enabled is False
    assert policy.effective_semantic_policy("prefer") == "disabled"
    assert policy.effective_semantic_policy("required") == "disabled"
    assert disabled_policy().effective_semantic_policy("prefer") == "prefer"


def test_f09_unauthorized_policy_mutation_is_rejected():
    from src.gateway.presentation.routers import management

    assert hasattr(management, "_require_mutation_tools")
    assert hasattr(management, "_require_knowledge_tools")
    with pytest.raises(AuthorizationException):
        management._require_mutation_tools(disabled_policy(mutation_tools_enabled=False))
    with pytest.raises(AuthorizationException):
        management._require_knowledge_tools(disabled_policy(knowledge_tools_enabled=False))
    management._require_mutation_tools(disabled_policy())
    management._require_knowledge_tools(disabled_policy())


def test_f09_ai_tool_discovery_matrix():
    from src.gateway.presentation.routers import management

    full = disabled_policy()
    assert management._ai_tool_allowed("knowledge.search.v1", full) is True
    assert management._ai_tool_allowed("retrieval.explain.v1", full) is True
    assert management._ai_tool_allowed("knowledge.create.v1", full) is True
    assert management._ai_tool_allowed("spaces.archive.v1", full) is True
    no_knowledge = disabled_policy(knowledge_tools_enabled=False)
    assert management._ai_tool_allowed("knowledge.search.v1", no_knowledge) is False
    assert management._ai_tool_allowed("retrieval.explain.v1", no_knowledge) is False
    assert management._ai_tool_allowed("knowledge.read.v1", no_knowledge) is False
    no_explain = disabled_policy(retrieval_explanations_enabled=False)
    assert management._ai_tool_allowed("retrieval.explain.v1", no_explain) is False
    assert management._ai_tool_allowed("knowledge.search.v1", no_explain) is True
    no_mutation = disabled_policy(mutation_tools_enabled=False)
    assert management._ai_tool_allowed("knowledge.create.v1", no_mutation) is False
    assert management._ai_tool_allowed("knowledge.update.v1", no_mutation) is False
    assert management._ai_tool_allowed("spaces.archive.v1", no_mutation) is False
    assert management._ai_tool_allowed("spaces.members.set.v1", no_mutation) is False
    assert management._ai_tool_allowed("knowledge.archive.v1", no_mutation) is False
    assert management._ai_tool_allowed("ingestion_jobs.cancel.v1", no_mutation) is False
    assert management._ai_tool_allowed("settings.propose.v1", no_mutation) is False
    assert management._ai_tool_allowed("spaces.list.v1", no_mutation) is True


def test_f09_explanation_redaction_keeps_schema_shape():
    from src.gateway.presentation.routers import management

    payload = {
        "query": "q",
        "hits": [],
        "health": {
            "semantic_status": "active",
            "degraded_reasons": ["x"],
            "embedding_generation_id": "gid",
            "embedding_coverage": 0.5,
        },
        "explanation": {"effective_space_ids": ["a"], "abstained": True, "active_space_id": "a"},
    }
    redacted = management._redact_explanation(payload)
    assert redacted["health"] == {
        "semantic_status": "active",
        "degraded_reasons": [],
        "embedding_generation_id": None,
        "embedding_coverage": None,
    }
    assert redacted["explanation"] == {"effective_space_ids": [], "abstained": True, "active_space_id": None}


# ---------------------------------------------------------------------------
# F10: typed review payload, hash binding, redaction
# ---------------------------------------------------------------------------


def test_f10_membership_review_distinguishes_reader_editor_removal():
    command = {"space_id": "family", "member_id": "m1", "expected_space_revision": 2}
    live_reader = {"space_name": "Family", "target_name": "Ava", "current_role": "reader", "authorized": True}
    reader = build_action_review(
        "spaces.members.set.v1", {**command, "role": "reader"}, live_reader, include_sensitive=True
    )
    editor = build_action_review(
        "spaces.members.set.v1", {**command, "role": "editor"}, live_reader, include_sensitive=True
    )
    removal = build_action_review(
        "spaces.members.set.v1", {**command, "role": None}, live_reader, include_sensitive=True
    )
    assert reader["change"]["after"]["role"] == "reader"
    assert editor["change"]["after"]["role"] == "editor"
    assert removal["change"]["after"]["role"] is None
    assert reader["change"]["before"]["role"] == "reader"
    assert "revokes" in removal["impact"]
    assert "revokes" not in reader["impact"]
    assert reader["summary"] != editor["summary"] != removal["summary"]


def test_f10_settings_review_shows_values_and_redacts_for_unauthorized_viewers():
    command = {
        "base_revision": 2,
        "values": {"retrieval": {"limit": 5}, "chunking": {}, "tools": {}, "features": {}},
        "reason": "tune retrieval",
    }
    live = {"active_values": {"retrieval": {"limit": 10}, "chunking": {}, "tools": {}, "features": {}}}
    shown = build_action_review("settings.propose.v1", command, live, include_sensitive=True)
    assert shown["change"]["after"]["values"] == command["values"]
    assert "retrieval" in shown["change"]["after"]["changed_groups"]
    assert shown["redacted"] == []
    hidden = build_action_review("settings.propose.v1", command, live, include_sensitive=False)
    assert hidden["change"]["after"]["values"] == REDACTED
    assert hidden["redacted"] == ["values"]
    assert hidden["impact"] == shown["impact"]


def test_f10_knowledge_archive_redacts_title_without_read_access():
    command = {"item_id": str(uuid4()), "expected_version": 3}
    live = {"title": "Secret plan", "version": 3, "authorized": True}
    shown = build_action_review("knowledge.archive.v1", command, live, include_sensitive=True)
    assert shown["change"]["before"]["title"] == "Secret plan"
    hidden = build_action_review("knowledge.archive.v1", command, live, include_sensitive=False)
    assert hidden["change"]["before"]["title"] == REDACTED
    assert hidden["redacted"] == ["title"]


def test_f10_review_is_bound_to_exact_command_hash():
    command = {"space_id": "family", "expected_revision": 2}
    original = IdempotencyService.request_hash({"tool_name": "spaces.archive.v1", "arguments": command})
    tampered = IdempotencyService.request_hash(
        {"tool_name": "spaces.archive.v1", "arguments": {**command, "space_id": "private"}}
    )
    assert original != tampered
    assert len(original) == 64


def test_f10_review_endpoint_is_declared_in_openapi():
    from src.gateway.main import create_app

    schema = create_app().openapi()
    operation = schema["paths"]["/api/v1/ai-actions/{action_id}"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PendingAIActionDetail"
    }
    detail = schema["components"]["schemas"]["PendingAIActionDetail"]
    assert "command_hash" in detail["properties"]
    assert "review" in detail["properties"]


# ---------------------------------------------------------------------------
# Management helper coverage for tag plumbing
# ---------------------------------------------------------------------------


def test_ai_search_arguments_accept_tags():
    from src.gateway.presentation.routers import management

    args = management.AIKnowledgeSearchArguments.model_validate({"query": "q", "tags": ["ops"]})
    assert args.tags == ["ops"]
    search = management.RetrievalSearchRequest.model_validate({"query": "q", "tags": ["ops"]})
    assert search.tags == ["ops"]
