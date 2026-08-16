"""Regression tests for production bugs found during systematic debugging (2026-08-16).

Each test documents one confirmed bug (reproduced against the live LLM backend
or via deterministic scripts) and locks in the fixed contract:

- BUG A: Streaming drops text deltas emitted before an external tool call.
- BUG B: canonical finish_reason ``max_tokens`` leaks into / is mangled by the
  OpenAI protocol surface (must map to ``length``); upstream ``length`` must
  normalize to canonical ``max_tokens``.
- BUG C: Anthropic SSE ``message_delta.stop_reason`` never reflects the
  orchestrator guardrail (``max_tokens``).
- BUG D: A user message containing both tool_result blocks and text drops the
  tool results when forwarded upstream.
- BUG E: Streaming parallel tool calls are merged because the upstream
  ``index`` field is discarded and reconstruction keys by list position.
- BUG F: ``tool_choice`` is never forwarded to the upstream LLM backend.
- BUG G: Auth public-path matching treats any ``/docs*`` prefix as public.
- BUG J: Anthropic SSE emits one tool_use content block per upstream delta
  chunk (fragmented JSON, fabricated ids) instead of accumulating per index.
- Usage: OpenAI SSE chunks never carry ``usage`` and the upstream stream
  request never asks for it (``stream_options.include_usage``).
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
import pytest

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.application.services.chat_orchestrator import ChatOrchestratorService
from src.gateway.domain.canonical import (
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalLLMStreamChunk,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
)
from src.gateway.domain.tools import FunctionCall, ToolCall


# ==============================================================================
# Shared scripted LLM client
# ==============================================================================


class ScriptedStreamLLM(ILLMClient):
    """Deterministic LLM client yielding scripted stream turns."""

    def __init__(self, scripted_turns: Optional[List[List[CanonicalLLMStreamChunk]]] = None):
        self.scripted_turns = list(scripted_turns or [])
        self.calls: List[Dict[str, Any]] = []

    async def generate(self, messages, tools=None, model=None, temperature=None, max_tokens=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        turn = self.scripted_turns.pop(0)
        text = "".join(c.delta_content or "" for c in turn)
        finish = next((c.finish_reason for c in reversed(turn) if c.finish_reason), "stop")
        from src.gateway.domain.canonical import CanonicalLLMResponse

        return CanonicalLLMResponse(id="resp-x", model=model or "m", content=text, finish_reason=finish)

    async def generate_stream(self, messages, tools=None, model=None, temperature=None, max_tokens=None, **kwargs):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "kwargs": kwargs,
            "stream": True,
        })
        turn = self.scripted_turns.pop(0)
        for chunk in turn:
            yield chunk


def _tool_delta(
    index: Optional[int],
    name: str,
    args: str,
    call_id: Optional[str],
) -> CanonicalLLMStreamChunk:
    """Build an upstream stream chunk carrying one tool-call delta."""
    return CanonicalLLMStreamChunk(
        id="chatcmpl-x",
        model="m",
        delta_tool_calls=[
            ToolCall(id=call_id or "", function=FunctionCall(name=name, arguments=args), index=index)
        ],
    )


def _collect_stream(orch, request) -> List[CanonicalStreamChunk]:
    async def run():
        return [c async for c in orch.orchestrate_chat_stream(request)]

    return asyncio_run(run())


def asyncio_run(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


# ==============================================================================
# BUG A: buffered text before external tool call must be flushed
# ==============================================================================


@pytest.mark.asyncio
async def test_bug_a_stream_text_before_external_tool_is_not_dropped():
    llm = ScriptedStreamLLM(
        [
            [
                CanonicalLLMStreamChunk(id="c1", model="m", delta_content="Let me check the weather."),
                _tool_delta(0, "get_weather", '{"city": "Paris"}', "call_1"),
                CanonicalLLMStreamChunk(id="c1", model="m", finish_reason="tool_calls"),
            ]
        ]
    )
    orch = ChatOrchestratorService(llm_client=llm, max_tool_iterations=3)
    request = CanonicalChatRequest(
        model="m",
        messages=[CanonicalMessage(role="user", content=[CanonicalTextBlock(text="weather?")])],
        stream=True,
    )

    collected = [c async for c in orch.orchestrate_chat_stream(request)]

    streamed_text = "".join(c.delta_content or "" for c in collected)
    assert "Let me check the weather." in streamed_text, (
        "Text streamed before an external tool call must still be delivered to the client"
    )
    assert any(c.delta_tool_calls for c in collected)


# ==============================================================================
# BUG B: finish_reason mapping between canonical and OpenAI protocol
# ==============================================================================


@pytest.mark.asyncio
async def test_bug_b_openai_nonstream_max_tokens_maps_to_length():
    from src.gateway.presentation.converters.openai_converter import canonical_response_to_openai

    resp = CanonicalChatResponse(
        id="r", model="m", content=[CanonicalTextBlock(text="hi")], finish_reason="max_tokens"
    )
    out = canonical_response_to_openai(resp)
    assert out.choices[0].finish_reason == "length", (
        "OpenAI protocol finish_reason for truncation is 'length', not the canonical 'max_tokens'"
    )


@pytest.mark.asyncio
async def test_bug_b_openai_stream_converter_max_tokens_maps_to_length():
    from src.gateway.presentation.converters.openai_converter import canonical_stream_chunk_to_openai

    chunk = CanonicalStreamChunk(id="r", model="m", delta_content="hi", finish_reason="max_tokens")
    out = canonical_stream_chunk_to_openai(chunk)
    assert out.choices[0].finish_reason == "length"


@pytest.mark.asyncio
async def test_bug_b_upstream_length_normalizes_to_canonical_max_tokens():
    """HttpLLMClient must translate upstream OpenAI 'length' into canonical 'max_tokens'."""
    from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient

    upstream_body = {
        "id": "chatcmpl-1",
        "model": "m",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "partial"}, "finish_reason": "length"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=upstream_body)

    client = HttpLLMClient(
        base_url="http://mock.test/v1", api_key="k", default_model="m", client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
    )
    resp = await client.generate(messages=[{"role": "user", "content": "hi"}])
    assert resp.finish_reason == "max_tokens"


# ==============================================================================
# BUG C + BUG J: Anthropic SSE stop_reason and tool block accumulation
# ==============================================================================


class _OrchProxy:
    """Wraps scripted chunks as an orchestrator for router dependency override."""

    def __init__(self, chunks: List[CanonicalStreamChunk]):
        self.chunks = chunks

    async def orchestrate_chat(self, request, workspace_id="global"):
        text = "".join(c.delta_content or "" for c in self.chunks)
        finish = next((c.finish_reason for c in self.chunks if c.finish_reason), "stop")
        return CanonicalChatResponse(
            id="msg_x", model=request.model, content=[CanonicalTextBlock(text=text)], finish_reason=finish
        )

    async def orchestrate_chat_stream(self, request, workspace_id="global"):
        for c in self.chunks:
            yield c


def _gateway_client(orch) -> httpx.AsyncClient:
    """Build an ASGI client against the real gateway app with a scripted orchestrator."""
    from src.gateway.main import create_app
    from src.gateway.presentation.routers import chat_completions, messages

    app = create_app()
    app.dependency_overrides[chat_completions.get_chat_orchestrator] = lambda: orch
    app.dependency_overrides[messages.get_chat_orchestrator] = lambda: orch
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _auth_headers() -> Dict[str, str]:
    from src.gateway.config import get_settings

    key = get_settings().gateway.gateway_api_keys[0]
    return {"Authorization": f"Bearer {key}"}


def _parse_sse_events(text: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    current_event: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            current_event = None
            continue
        if line.startswith("event: "):
            current_event = line[len("event: "):]
        elif line.startswith("data: "):
            payload = line[len("data: "):]
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {"raw": payload}
            data["_event"] = current_event
            events.append(data)
    return events


@pytest.mark.asyncio
async def test_bug_c_anthropic_stream_stop_reason_max_tokens():
    chunks = [
        CanonicalStreamChunk(id="m", model="m", delta_content="warn", finish_reason="max_tokens"),
    ]
    client = _gateway_client(_OrchProxy(chunks))
    try:
        resp = await client.post(
            "/v1/messages",
            headers=_auth_headers(),
            json={
                "model": "claude-3",
                "max_tokens": 100,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        message_deltas = [e for e in events if e.get("_event") == "message_delta"]
        assert message_deltas, "message_delta event must be present"
        assert message_deltas[-1]["delta"]["stop_reason"] == "max_tokens", (
            "Anthropic SSE must surface the orchestrator guardrail stop_reason"
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_bug_j_anthropic_stream_accumulates_tool_use_block():
    """Fragmented external tool deltas must produce ONE tool_use block with joined JSON."""
    chunks = [
        CanonicalStreamChunk(
            id="m",
            model="m",
            delta_tool_calls=[ToolCall(id="toolu_01", function=FunctionCall(name="get_weather", arguments="{"), index=0)],
        ),
        CanonicalStreamChunk(
            id="m",
            model="m",
            delta_tool_calls=[ToolCall(id="", function=FunctionCall(name="", arguments='"city":"Paris"'), index=0)],
        ),
        CanonicalStreamChunk(
            id="m",
            model="m",
            delta_tool_calls=[ToolCall(id="", function=FunctionCall(name="", arguments="}"), index=0)],
        ),
        CanonicalStreamChunk(id="m", model="m", finish_reason="tool_use"),
    ]
    client = _gateway_client(_OrchProxy(chunks))
    try:
        resp = await client.post(
            "/v1/messages",
            headers=_auth_headers(),
            json={
                "model": "claude-3",
                "max_tokens": 100,
                "stream": True,
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "w",
                        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                    }
                ],
            },
        )
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        starts = [e for e in events if e.get("_event") == "content_block_start" and e["content_block"]["type"] == "tool_use"]
        stops = [e for e in events if e.get("_event") == "content_block_stop"]
        deltas = [e for e in events if e.get("_event") == "content_block_delta" and e["delta"]["type"] == "input_json_delta"]

        assert len(starts) == 1, f"Expected exactly one tool_use start, got {len(starts)}"
        assert starts[0]["content_block"]["id"] == "toolu_01"
        assert starts[0]["content_block"]["name"] == "get_weather"
        joined = "".join(d["delta"]["partial_json"] for d in deltas)
        assert json.loads(joined) == {"city": "Paris"}
        assert len(stops) >= 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_bug_b_router_openai_sse_maps_max_tokens_to_length():
    chunks = [CanonicalStreamChunk(id="c", model="m", delta_content="partial", finish_reason="max_tokens")]
    client = _gateway_client(_OrchProxy(chunks))
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers=_auth_headers(),
            json={
                "model": "gpt-test",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 200
        events = _parse_sse_events(resp.text)
        finish_reasons = [
            e["choices"][0]["finish_reason"]
            for e in events
            if e.get("choices") and e["choices"][0].get("finish_reason")
        ]
        assert finish_reasons, "stream must terminate with a finish_reason"
        assert finish_reasons[-1] == "length", (
            f"OpenAI SSE finish_reason must map canonical max_tokens -> length, got {finish_reasons[-1]!r}"
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_openai_sse_forwards_usage_when_present():
    usage = CanonicalUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    chunks = [
        CanonicalStreamChunk(id="c", model="m", delta_content="hi", finish_reason="stop", usage=usage),
    ]
    client = _gateway_client(_OrchProxy(chunks))
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers=_auth_headers(),
            json={"model": "gpt-test", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        events = _parse_sse_events(resp.text)
        with_usage = [e for e in events if e.get("usage")]
        assert with_usage, "OpenAI SSE must forward usage when the orchestrator provides it"
        assert with_usage[-1]["usage"]["total_tokens"] == 18
    finally:
        await client.aclose()


# ==============================================================================
# BUG D: mixed tool_result + text user messages must keep both parts
# ==============================================================================


@pytest.mark.asyncio
async def test_bug_d_mixed_user_message_keeps_tool_result_and_text():
    llm = ScriptedStreamLLM(
        [[CanonicalLLMStreamChunk(id="c", model="m", delta_content="done", finish_reason="stop")]]
    )
    orch = ChatOrchestratorService(llm_client=llm, max_tool_iterations=3)
    request = CanonicalChatRequest(
        model="m",
        messages=[
            CanonicalMessage(
                role="assistant",
                content=[CanonicalToolUseBlock(id="t1", name="get_weather", input={"city": "Paris"})],
            ),
            CanonicalMessage(
                role="user",
                content=[
                    CanonicalToolResultBlock(tool_use_id="t1", content="18C"),
                    CanonicalTextBlock(text="Now summarize."),
                ],
            ),
        ],
        stream=True,
    )
    async for _ in orch.orchestrate_chat_stream(request):
        pass

    upstream = llm.calls[0]["messages"]
    tool_msgs = [m for m in upstream if m.get("role") == "tool"]
    user_msgs = [m for m in upstream if m.get("role") == "user" and m.get("content")]

    assert any(m.get("tool_call_id") == "t1" and "18C" in str(m.get("content")) for m in tool_msgs), (
        "tool_result content must be forwarded as a tool-role message"
    )
    assert any(str(m.get("content")) == "Now summarize." for m in user_msgs), (
        "user text must still be forwarded after the tool result"
    )


# ==============================================================================
# BUG E: streaming parallel tool calls must not merge
# ==============================================================================


@pytest.mark.asyncio
async def test_bug_e_parallel_streamed_tool_calls_are_not_merged():
    llm = ScriptedStreamLLM(
        [
            [
                _tool_delta(0, "knowledge_search", '{"query":', "call_a"),
                _tool_delta(0, "", ' "deploy"}', None),
                _tool_delta(1, "knowledge_search", '{"query":', "call_b"),
                _tool_delta(1, "", ' "rollback"}', None),
                CanonicalLLMStreamChunk(id="c", model="m", finish_reason="tool_calls"),
            ],
            [CanonicalLLMStreamChunk(id="c", model="m", delta_content="done", finish_reason="stop")],
        ]
    )

    executed: List[Dict[str, Any]] = []

    class NullKnowledge:
        async def execute_tool(self, tool_call_id, name, arguments, session_workspace_id=None):
            from src.gateway.domain.tools import ToolResult

            executed.append({"id": tool_call_id, "args": arguments})
            return ToolResult(tool_call_id=tool_call_id, name=name, content="{}", is_error=False)

    orch = ChatOrchestratorService(llm_client=llm, knowledge_service=NullKnowledge(), max_tool_iterations=3)
    request = CanonicalChatRequest(
        model="m",
        messages=[CanonicalMessage(role="user", content=[CanonicalTextBlock(text="go")])],
        stream=True,
    )
    collected = [c async for c in orch.orchestrate_chat_stream(request)]
    assert collected, "expected a streamed response"

    # Both internal tool calls must have executed separately with intact args.
    assert len(executed) == 2, f"Two parallel internal tool calls must execute, got {len(executed)}: {executed}"
    assert executed[0]["id"] == "call_a" and executed[0]["args"] == {"query": "deploy"}
    assert executed[1]["id"] == "call_b" and executed[1]["args"] == {"query": "rollback"}

    # The follow-up turn must carry the two assistant tool_calls unmerged.
    stream_calls = [c for c in llm.calls if c.get("stream")]
    assert len(stream_calls) == 2, "internal tool loop must issue a follow-up LLM call"
    followup_msgs = stream_calls[1]["messages"]
    assistant_msgs = [m for m in followup_msgs if m.get("role") == "assistant" and m.get("tool_calls")]
    assert assistant_msgs, "follow-up turn must contain the assistant tool_calls"
    tcs = assistant_msgs[-1]["tool_calls"]
    assert len(tcs) == 2, f"Two parallel tool calls must remain separate, got {len(tcs)}: {tcs}"
    assert tcs[0]["function"]["name"] == "knowledge_search"
    assert tcs[1]["function"]["name"] == "knowledge_search"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"query": "deploy"}
    assert json.loads(tcs[1]["function"]["arguments"]) == {"query": "rollback"}


@pytest.mark.asyncio
async def test_bug_e_client_preserves_tool_call_index_and_sparse_ids():
    """HttpLLMClient stream parsing must keep the upstream tool index and not fabricate ids."""
    from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient

    def sse_body() -> str:
        chunks = [
            {
                "id": "c1",
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_a", "type": "function",
                                 "function": {"name": "fn_a", "arguments": "{}"}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "c1",
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 1, "id": "call_b", "type": "function",
                                 "function": {"name": "fn_b", "arguments": "{\"x\":"}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "c1",
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 1, "function": {"arguments": "1}"}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
        ]
        lines = [f"data: {json.dumps(c)}" for c in chunks]
        lines.append("data: [DONE]")
        return "\n\n".join(lines) + "\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse_body().encode("utf-8"), headers={"content-type": "text/event-stream"}
        )

    client = HttpLLMClient(
        base_url="http://mock.test/v1", api_key="k", default_model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    parsed = [c async for c in client.generate_stream(messages=[{"role": "user", "content": "hi"}])]

    tool_chunks = [c for c in parsed if c.delta_tool_calls]
    assert len(tool_chunks) == 3
    first = tool_chunks[0].delta_tool_calls[0]
    second = tool_chunks[1].delta_tool_calls[0]
    third = tool_chunks[2].delta_tool_calls[0]

    assert getattr(first, "index", None) == 0, "index field must be preserved on streamed tool deltas"
    assert getattr(second, "index", None) == 1
    assert getattr(third, "index", None) == 1
    assert not third.id, "continuation deltas without an id must not get a fabricated id"
    assert third.function.name == "", "continuation deltas keep an empty name"

    # Mid-stream chunks must keep an unset finish reason; only the terminal
    # chunk carries one (mapped from upstream 'length' to canonical
    # 'max_tokens' where applicable).
    for c in tool_chunks:
        assert c.finish_reason is None


@pytest.mark.asyncio
async def test_client_stream_mid_chunks_keep_null_finish_reason():
    """Regression: normalization must not invent 'stop' on mid-stream chunks."""
    from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient

    def build_sse(final_reason: Optional[str]) -> bytes:
        payloads = [
            {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "he"}, "finish_reason": None}]},
            {"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": final_reason}]},
        ]
        lines = [f"data: {json.dumps(p)}" for p in payloads]
        lines.append("data: [DONE]")
        return ("\n\n".join(lines) + "\n\n").encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=build_sse("length"), headers={"content-type": "text/event-stream"}
        )

    client = HttpLLMClient(
        base_url="http://mock.test/v1", api_key="k", default_model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    parsed = [c async for c in client.generate_stream(messages=[{"role": "user", "content": "hi"}])]

    assert [c.finish_reason for c in parsed] == [None, "max_tokens"], (
        "mid-stream finish_reason must stay None; upstream 'length' must normalize to canonical 'max_tokens'"
    )


# ==============================================================================
# BUG F: tool_choice forwarding
# ==============================================================================


@pytest.mark.asyncio
async def test_bug_f_orchestrator_forwards_tool_choice():
    captured: Dict[str, Any] = {}

    class CapturingLLM(ILLMClient):
        async def generate(self, messages, tools=None, model=None, temperature=None, max_tokens=None, **kwargs):
            captured.update(kwargs)
            captured["tools"] = tools
            from src.gateway.domain.canonical import CanonicalLLMResponse

            return CanonicalLLMResponse(id="r", model=model or "m", content="ok", finish_reason="stop")

        async def generate_stream(self, *a, **kw):  # pragma: no cover - unused
            raise NotImplementedError

    orch = ChatOrchestratorService(llm_client=CapturingLLM(), max_tool_iterations=2)
    request = CanonicalChatRequest(
        model="m",
        messages=[CanonicalMessage(role="user", content=[CanonicalTextBlock(text="hi")])],
        tool_choice={"type": "function", "function": {"name": "bash"}},
    )
    await orch.orchestrate_chat(request)
    assert captured.get("tool_choice") == {"type": "function", "function": {"name": "bash"}}, (
        "orchestrator must forward the client tool_choice to the upstream LLM"
    )


@pytest.mark.asyncio
async def test_bug_f_client_payload_includes_tool_choice():
    from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient

    captured: Dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"id": "x", "model": "m", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]},
        )

    client = HttpLLMClient(
        base_url="http://mock.test/v1", api_key="k", default_model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await client.generate(
        messages=[{"role": "user", "content": "hi"}],
        tool_choice={"type": "function", "function": {"name": "bash"}},
    )
    assert captured["payload"].get("tool_choice") == {"type": "function", "function": {"name": "bash"}}


@pytest.mark.asyncio
async def test_bug_f_anthropic_tool_choice_mapped_to_openai_format():
    from src.gateway.presentation.converters.anthropic_converter import anthropic_request_to_canonical
    from src.gateway.presentation.schemas.anthropic_schemas import AnthropicMessagesRequest

    req = AnthropicMessagesRequest(
        model="claude-3",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        tool_choice={"type": "tool", "name": "get_weather"},
    )
    canonical = anthropic_request_to_canonical(req)
    assert canonical.tool_choice == {"type": "function", "function": {"name": "get_weather"}}

    req2 = AnthropicMessagesRequest(
        model="claude-3",
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
        tool_choice={"type": "any"},
    )
    canonical2 = anthropic_request_to_canonical(req2)
    assert canonical2.tool_choice == "required"


@pytest.mark.asyncio
async def test_stream_options_include_usage_requested_from_upstream():
    from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient

    captured: Dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        body = "\n\n".join(
            [
                json.dumps({"id": "c", "model": "m", "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]}),
                json.dumps({"id": "c", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
                json.dumps({"id": "c", "model": "m", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}),
                "data: [DONE]",
            ]
        )
        return httpx.Response(
            200,
            content=("data: " + body.replace("\n\n", "\n\ndata: ")).encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    client = HttpLLMClient(
        base_url="http://mock.test/v1", api_key="k", default_model="m",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    parsed = [c async for c in client.generate_stream(messages=[{"role": "user", "content": "hi"}])]
    assert captured["payload"].get("stream_options") == {"include_usage": True}, (
        "gateway must request usage accounting from streaming backends"
    )
    usage_chunks = [c for c in parsed if c.usage]
    assert usage_chunks and usage_chunks[-1].usage.total_tokens == 5


# ==============================================================================
# BUG G: auth public path prefix must not match unrelated prefixes
# ==============================================================================


def test_bug_g_public_path_prefix_is_strict():
    from src.gateway.presentation.auth import is_public_path

    assert is_public_path("/health") is True
    assert is_public_path("/health/") is True
    assert is_public_path("/docs") is True
    assert is_public_path("/openapi.json") is True
    assert is_public_path("/docsx") is False, "unrelated paths sharing a prefix must not bypass auth"
    assert is_public_path("/docs/") is True
    assert is_public_path("/docs/oauth2-redirect") is True, "FastAPI doc sub-routes stay public"
    assert is_public_path("/documentation") is False


# ==============================================================================
# BUG H: document is_global must be persisted and honored by search scoping
# ==============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bug_h_document_is_global_persisted_and_searchable():
    from src.gateway.application.services.ingestion_service import IngestionService
    from src.gateway.infrastructure.persistence.document_repository import DocumentRepository
    from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter
    from tests.e2e.harness.test_env import TestEnvironment

    async with TestEnvironment() as env:
        storage = LocalStorageAdapter(base_dir=env.storage_path)
        repo = DocumentRepository(session_factory=env.session_factory)
        service = IngestionService(storage=storage, document_repository=repo, embedding_client=None)

        doc = await service.ingest_file(
            workspace_id="team_a",
            filename="shared.txt",
            content=b"zeta protocol quantum handshake resonates globally",
            is_global=True,
        )
        assert doc.is_global is True, "is_global must round-trip through persistence"

        results = await repo.search_chunks_fts(
            query="zeta protocol",
            workspace_id="team_b",
            limit=5,
        )
        assert any(r.id for r in results), (
            "A document ingested with is_global=true must be discoverable from other workspaces"
        )
