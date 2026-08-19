"""Regression tests for bugs found during live-backend systematic debugging (sprint 2).

Each test documents one confirmed bug (reproduced against the live LLM backend
at localhost:8888 or via deterministic race injection) and locks in the fixed
contract:

- BUG 1: OpenAI SSE stream mixes chunk ``id`` / ``model`` values (synthetic
  first chunk vs upstream chunks). Per the OpenAI streaming spec every chunk
  of one completion must share the same id and model.
- BUG 2: The OpenAI stream terminal usage-only chunk must carry an empty
  ``choices`` list (``stream_options.include_usage`` contract), not a filler
  choice with an empty delta.
- BUG 3: ``messages: []`` was forwarded verbatim to the upstream backend
  (llama-server Jinja 500 surfaced as a confusing gateway 502) instead of
  being rejected as a 400 invalid_request_error on both protocols.
- BUG 4: OCC update race surfaces a raw IntegrityError (unique revision
  version violation) instead of the 409 ConcurrencyConflictException.
- BUG 5: Anthropic SSE ignores real token usage; ``input_tokens`` is always 0
  and ``output_tokens`` is estimated by chars/4 instead of reported usage.
- BUG 6: OpenAI ``developer`` role is rejected (422) instead of being treated
  as a system message.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.config import Settings, settings
from src.gateway.domain.canonical import (
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalUsage,
)
from src.gateway.domain.entities import KnowledgeRevision
from src.gateway.domain.exceptions import ConcurrencyConflictException
from src.gateway.infrastructure.persistence.knowledge_repository import KnowledgeRepository
from src.gateway.infrastructure.persistence.models import (
    KnowledgeRevision as ORMKnowledgeRevision,
)
from src.gateway.main import create_app
from src.gateway.presentation.routers.chat_completions import (
    get_llm_client as get_openai_llm_client,
)
from src.gateway.presentation.routers.messages import (
    get_llm_client as get_anthropic_llm_client,
)
from tests.e2e.harness.test_env import TestEnvironment


# ==============================================================================
# Shared scripted LLM client & app helpers
# ==============================================================================


class ScriptedStreamLLM(ILLMClient):
    """Deterministic LLM client yielding scripted stream chunks."""

    def __init__(self, chunks: List[CanonicalLLMStreamChunk]):
        self.chunks = list(chunks)
        self.calls: List[Dict[str, Any]] = []

    async def generate(self, messages, tools=None, model=None, temperature=None, max_tokens=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        return CanonicalLLMResponse(
            id="resp-scripted",
            model=model or "scripted-model",
            content="ok",
            finish_reason="stop",
        )

    async def generate_stream(self, messages, tools=None, model=None, temperature=None, max_tokens=None, **kwargs):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "kwargs": kwargs,
            "stream": True,
        })
        for chunk in self.chunks:
            yield chunk


class ExplodingLLM(ILLMClient):
    """LLM client that fails the test if the gateway ever calls upstream."""

    def __init__(self):
        self.calls = 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("upstream LLM must not be called for this request")

    async def generate_stream(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("upstream LLM must not be called for this request")
        yield  # pragma: no cover


def _make_app(llm: ILLMClient) -> httpx.AsyncClient:
    app = create_app(
        Settings(
            gateway={
                "environment": "test",
                "api_keys": ["live-regression-key"],
                "legacy_api_keys_enabled": True,
            }
        )
    )
    app.dependency_overrides[get_openai_llm_client] = lambda: llm
    app.dependency_overrides[get_anthropic_llm_client] = lambda: llm
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway-test",
        headers={"Authorization": "Bearer live-regression-key"},
    )


async def _parse_sse(response: httpx.Response) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    async for line in response.aiter_lines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            break
        events.append(json.loads(payload))
    return events


# ==============================================================================
# BUG 1: OpenAI SSE stream must use one consistent id and model
# ==============================================================================


@pytest.mark.asyncio
async def test_openai_stream_single_id_and_model():
    """All SSE chunks in one completion must share a single id and model."""
    chunks = [
        CanonicalLLMStreamChunk(id="upstream-abc", model="backend-real-model", delta_content="Hello"),
        CanonicalLLMStreamChunk(id="upstream-abc", model="backend-real-model", delta_content=" world"),
        CanonicalLLMStreamChunk(id="upstream-abc", model="backend-real-model", finish_reason="stop"),
    ]
    llm = ScriptedStreamLLM(chunks)
    async with _make_app(llm) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "alias-name",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        events = await _parse_sse(resp)

    assert len(events) >= 4
    chunk_ids = {e["id"] for e in events}
    chunk_models = {e["model"] for e in events}
    assert len(chunk_ids) == 1, f"stream chunks mixed ids: {chunk_ids}"
    assert len(chunk_models) == 1, f"stream chunks mixed models: {chunk_models}"


# ==============================================================================
# BUG 2: usage-only stream chunk must carry an empty choices list
# ==============================================================================


@pytest.mark.asyncio
async def test_openai_stream_usage_only_chunk_empty_choices():
    """The terminal usage chunk must render choices: [] per the include_usage contract."""
    chunks = [
        CanonicalLLMStreamChunk(id="upstream-abc", model="backend-real-model", delta_content="Hi"),
        CanonicalLLMStreamChunk(id="upstream-abc", model="backend-real-model", finish_reason="stop"),
        CanonicalLLMStreamChunk(
            id="upstream-abc",
            model="backend-real-model",
            usage=CanonicalUsage(prompt_tokens=42, completion_tokens=18, total_tokens=60),
        ),
    ]
    llm = ScriptedStreamLLM(chunks)
    async with _make_app(llm) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "alias-name",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        events = await _parse_sse(resp)

    usage_events = [e for e in events if e.get("usage")]
    assert usage_events, "stream must surface upstream usage"
    final_usage = usage_events[-1]
    assert final_usage["choices"] == [], (
        f"usage-only chunk must carry an empty choices list, got {final_usage['choices']}"
    )
    assert final_usage["usage"]["prompt_tokens"] == 42
    assert final_usage["usage"]["total_tokens"] == 60


# ==============================================================================
# BUG 3: empty messages must be a 400, never forwarded upstream
# ==============================================================================


@pytest.mark.asyncio
async def test_openai_empty_messages_returns_400():
    llm = ExplodingLLM()
    async with _make_app(llm) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "default", "messages": []},
        )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["type"] == "invalid_request_error"
    assert "messages" in data["error"]["message"].lower()
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_anthropic_empty_messages_returns_400():
    llm = ExplodingLLM()
    async with _make_app(llm) as client:
        resp = await client.post(
            "/v1/messages",
            json={"model": "default", "max_tokens": 64, "messages": []},
        )
    assert resp.status_code == 400
    data = resp.json()
    assert data["type"] == "error"
    assert data["error"]["type"] == "invalid_request_error"
    assert llm.calls == 0


# ==============================================================================
# BUG 4: OCC race must surface ConcurrencyConflictException, not IntegrityError
# ==============================================================================


class _RaceInjectingSessionFactory:
    """Session factory wrapper that injects a competing revision insert into the
    first flush, deterministically reproducing the OCC race window where two
    transactions both read the same current version and both try to write the
    next one."""

    def __init__(self, inner, item_id):
        self._inner = inner
        self._item_id = item_id
        self._injected = False

    def __call__(self):
        session = self._inner()
        factory = self

        @event.listens_for(session.sync_session, "before_flush")
        def _inject_race(sess, flush_context, instances):
            if factory._injected:
                return
            factory._injected = True
            sess.add(
                ORMKnowledgeRevision(
                    id=uuid4(),
                    item_id=factory._item_id,
                    version=2,
                    content_hash="0" * 64,
                    content="competing concurrent write",
                    author="racer",
                )
            )

        return session


@pytest.mark.asyncio
async def test_occ_race_duplicate_revision_maps_to_conflict():
    """When the unique (item_id, version) constraint fires mid-update the
    repository must translate it into a 409 ConcurrencyConflictException."""
    async with TestEnvironment() as env:
        base_repo = KnowledgeRepository(session_factory=env.session_factory)
        item_id = uuid4()
        item_created = await base_repo.create_item(
            item=__import__(
                "src.gateway.domain.entities", fromlist=["KnowledgeItem"]
            ).KnowledgeItem(
                id=item_id,
                workspace_id="global",
                title="OCC race item",
                content="v1",
            ),
            initial_revision=KnowledgeRevision(
                item_id=item_id,
                version=1,
                content="v1",
                content_hash=KnowledgeRevision.compute_hash("v1"),
            ),
        )
        assert item_created.version == 1

        racing_repo = KnowledgeRepository(
            session_factory=_RaceInjectingSessionFactory(env.session_factory, item_id)
        )
        with pytest.raises(ConcurrencyConflictException) as excinfo:
            await racing_repo.update_item_occ(
                item_id=item_id,
                expected_version=1,
                new_revision=KnowledgeRevision(
                    item_id=item_id,
                    version=2,
                    content="v2 from stale transaction",
                    content_hash=KnowledgeRevision.compute_hash("v2 from stale transaction"),
                ),
            )
        assert excinfo.value.status_code == 409


# ==============================================================================
# BUG 5: Anthropic SSE must report real usage when upstream provides it
# ==============================================================================


@pytest.mark.asyncio
async def test_anthropic_stream_reports_real_usage():
    chunks = [
        CanonicalLLMStreamChunk(id="upstream-abc", model="backend-real-model", delta_content="Bon"),
        CanonicalLLMStreamChunk(id="upstream-abc", model="backend-real-model", delta_content="jour"),
        CanonicalLLMStreamChunk(id="upstream-abc", model="backend-real-model", finish_reason="stop"),
        CanonicalLLMStreamChunk(
            id="upstream-abc",
            model="backend-real-model",
            usage=CanonicalUsage(prompt_tokens=42, completion_tokens=18, total_tokens=60),
        ),
    ]
    llm = ScriptedStreamLLM(chunks)
    async with _make_app(llm) as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "say bonjour"}],
            },
        )
        assert resp.status_code == 200
        events = await _parse_sse(resp)

    delta_events = [e for e in events if e.get("type") == "message_delta"]
    assert delta_events, "message_delta event missing"
    usage = delta_events[-1]["usage"]
    assert usage["output_tokens"] == 18, (
        f"must report real completion tokens, got {usage['output_tokens']}"
    )
    assert usage.get("input_tokens") == 42, (
        f"must report real prompt tokens, got {usage.get('input_tokens')}"
    )


# ==============================================================================
# BUG 6: OpenAI "developer" role must be accepted as a system-level message
# ==============================================================================


@pytest.mark.asyncio
async def test_openai_developer_role_accepted_as_system():
    chunks = [
        CanonicalLLMStreamChunk(id="upstream-abc", model="backend-real-model", delta_content="ok"),
        CanonicalLLMStreamChunk(id="upstream-abc", model="backend-real-model", finish_reason="stop"),
    ]
    llm = ScriptedStreamLLM(chunks)
    async with _make_app(llm) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [
                    {"role": "developer", "content": "You are a coding assistant."},
                    {"role": "user", "content": "hello"},
                ],
            },
        )
    assert resp.status_code == 200, f"developer role must be accepted: {resp.text}"
    assert llm.calls, "upstream must have been called once"
    forwarded_roles = [m["role"] for m in llm.calls[0]["messages"]]
    assert forwarded_roles[0] == "system", (
        f"developer message must be forwarded as system, got {forwarded_roles}"
    )
