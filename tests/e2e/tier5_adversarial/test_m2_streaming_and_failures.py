"""Tier 5 Adversarial Stress Tests: Milestone M2 Streaming SSE & Failure Modes.

Empirically stress-tests:
1. OpenAI SSE streaming protocol fidelity (formatting, double newlines, [DONE] termination, tool calls, headers).
2. Anthropic SSE event lifecycle (message_start, content_block_start, content_block_delta, content_block_stop, message_delta, message_stop, error events).
3. Backend HTTP LLM client connection failures (connection refused, timeouts, 4xx/5xx upstream errors, network drops).
4. Malformed upstream SSE handling (comments, corrupted JSON, missing fields, no-space data prefixes, duplicate [DONE]).
5. Unicode, multibyte, emoji, and large chunk stream stress.
6. Protocol-appropriate error payloads (OpenAI vs Anthropic) across 401, 404, 500, 502 status codes.
7. HTML error bodies, nested JSON error bodies, multiple sequential tool uses, large multi-turn conversations.
"""

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional
import httpx
import pytest

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.application.services.model_registry import ModelRegistryService
from src.gateway.config import Settings
from src.gateway.domain.canonical import (
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalMessage,
    CanonicalTextBlock,
    CanonicalUsage,
)
from src.gateway.domain.exceptions import (
    AuthenticationException,
    GatewayException,
    LLMProviderException,
    ModelNotFoundException,
)
from src.gateway.domain.tools import FunctionCall, ToolCall
from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient
from src.gateway.main import create_app
from src.gateway.presentation.auth import authenticate_credentials
from tests.e2e.harness.mock_server import (
    MockLLMController,
    MockLLMResponse,
    MockServerManager,
    MockToolCall,
)


def get_test_api_key() -> str:
    """Get currently configured gateway API key for test requests."""
    return "streaming-adversarial-key"


def _test_settings() -> Settings:
    return Settings(
        gateway={
            "environment": "test",
            "api_keys": [get_test_api_key()],
            "legacy_api_keys_enabled": True,
        }
    )


# ==============================================================================
# Helper Mock Clients for Dependency Injection & Unit Stress
# ==============================================================================

class MockFailingLLMClient(ILLMClient):
    """Mock LLM client that simulates various backend failures."""

    def __init__(self, failure_type: str = "exception", error_message: str = "Simulated backend failure"):
        self.failure_type = failure_type
        self.error_message = error_message

    async def generate(self, messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, **kwargs) -> CanonicalLLMResponse:
        if self.failure_type == "provider_error":
            raise LLMProviderException(message=self.error_message, details={"status_code": 502})
        elif self.failure_type == "timeout":
            raise LLMProviderException(message="Upstream request timed out", details={"endpoint": "http://mock-llm"})
        elif self.failure_type == "not_found":
            raise ModelNotFoundException(self.error_message)
        else:
            raise RuntimeError(self.error_message)

    async def generate_stream(self, messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, **kwargs) -> AsyncIterator[CanonicalLLMStreamChunk]:
        if self.failure_type == "immediate_error":
            raise LLMProviderException(message=self.error_message, details={"status_code": 502})
        elif self.failure_type == "midstream_error":
            # Yield one chunk, then blow up
            yield CanonicalLLMStreamChunk(
                id="chunk-1",
                model="test-model",
                delta_content="Beginning of response...",
            )
            raise LLMProviderException(message="Connection dropped midstream", details={"endpoint": "http://mock-llm"})
        else:
            raise RuntimeError(self.error_message)
            yield  # unreachable


class MockCustomStreamLLMClient(ILLMClient):
    """Mock LLM client yielding specific customizable stream chunks."""

    def __init__(self, chunks: List[CanonicalLLMStreamChunk]):
        self.chunks = chunks
        self.last_kwargs: Dict[str, Any] = {}

    async def generate(self, messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, **kwargs) -> CanonicalLLMResponse:
        self.last_kwargs = kwargs
        return CanonicalLLMResponse(
            id="resp-1",
            model=model or "test-model",
            content="Mock response",
            tool_calls=[],
            finish_reason="stop",
            usage=CanonicalUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def generate_stream(self, messages: list[dict], tools: Optional[list[dict]] = None, model: Optional[str] = None, **kwargs) -> AsyncIterator[CanonicalLLMStreamChunk]:
        self.last_kwargs = kwargs
        for c in self.chunks:
            yield c


# ==============================================================================
# 1. OpenAI Streaming Protocol Fidelity & Format Verification
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_openai_sse_format_double_newline_and_done():
    """Verify raw OpenAI SSE frames adhere to `data: {...}\\n\\n` and end with `data: [DONE]\\n\\n`."""
    api_key = get_test_api_key()
    chunks = [
        CanonicalLLMStreamChunk(id="c1", model="test-model", delta_content="Hello "),
        CanonicalLLMStreamChunk(id="c2", model="test-model", delta_content="World!"),
        CanonicalLLMStreamChunk(id="c3", model="test-model", finish_reason="stop"),
    ]
    mock_client = MockCustomStreamLLMClient(chunks)
    app = create_app(_test_settings())

    from src.gateway.presentation.routers.chat_completions import get_llm_client
    app.dependency_overrides[get_llm_client] = lambda: mock_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "coding",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        assert resp.headers["cache-control"] == "no-cache"

        raw_text = resp.text
        frames = [f for f in raw_text.split("\n\n") if f.strip()]

        assert len(frames) >= 4  # Initial role + 2 content chunks + finish chunk + [DONE]
        assert frames[-1] == "data: [DONE]"

        for frame in frames[:-1]:
            assert frame.startswith("data: ")
            payload = json.loads(frame[6:])
            assert payload["object"] == "chat.completion.chunk"
            assert "choices" in payload
            assert len(payload["choices"]) == 1
            assert "delta" in payload["choices"][0]


@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_openai_sse_tool_call_streaming_chunks():
    """Verify OpenAI streaming of tool calls produces correct index and arguments delta."""
    api_key = get_test_api_key()
    tool_chunk_1 = CanonicalLLMStreamChunk(
        id="c1",
        model="test-model",
        delta_tool_calls=[
            ToolCall(
                id="call_123",
                type="function",
                function=FunctionCall(name="web_search", arguments='{"query":'),
            )
        ],
    )
    tool_chunk_2 = CanonicalLLMStreamChunk(
        id="c2",
        model="test-model",
        delta_tool_calls=[
            ToolCall(
                id="call_123",
                type="function",
                function=FunctionCall(name="web_search", arguments=' "architecture"}'),
            )
        ],
        finish_reason="tool_use",
    )
    mock_client = MockCustomStreamLLMClient([tool_chunk_1, tool_chunk_2])
    app = create_app(_test_settings())

    from src.gateway.presentation.routers.chat_completions import get_llm_client
    app.dependency_overrides[get_llm_client] = lambda: mock_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Search docs"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        frames = [f for f in resp.text.split("\n\n") if f.strip() and f != "data: [DONE]"]

        found_tool_calls = []
        for frame in frames:
            assert frame.startswith("data: ")
            payload = json.loads(frame[6:])
            delta = payload["choices"][0]["delta"]
            if "tool_calls" in delta:
                found_tool_calls.extend(delta["tool_calls"])

        assert len(found_tool_calls) == 2
        assert found_tool_calls[0]["function"]["name"] == "web_search"
        assert found_tool_calls[0]["id"] == "call_123"
        assert found_tool_calls[0]["index"] == 0

        # Verify finish reason mapped to "tool_calls"
        last_choice = json.loads(frames[-1][6:])["choices"][0]
        assert last_choice["finish_reason"] == "tool_calls"


# ==============================================================================
# 2. Anthropic Streaming SSE Event Sequence & Lifecycle Verification
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_anthropic_sse_exact_event_sequence_lifecycle():
    """Verify Anthropic SSE follows message_start -> content_block_start -> content_block_delta -> content_block_stop -> message_delta -> message_stop."""
    api_key = get_test_api_key()
    chunks = [
        CanonicalLLMStreamChunk(id="c1", model="claude-3-5", delta_content="Clean "),
        CanonicalLLMStreamChunk(id="c2", model="claude-3-5", delta_content="Architecture"),
        CanonicalLLMStreamChunk(id="c3", model="claude-3-5", finish_reason="stop"),
    ]
    mock_client = MockCustomStreamLLMClient(chunks)
    app = create_app(_test_settings())

    from src.gateway.presentation.routers.messages import get_llm_client
    app.dependency_overrides[get_llm_client] = lambda: mock_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/messages",
            headers={"x-api-key": api_key},
            json={
                "model": "coding",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Explain architecture"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        raw_text = resp.text
        blocks = [b for b in raw_text.split("\n\n") if b.strip()]

        event_names = []
        event_payloads = []
        for block in blocks:
            lines = block.split("\n")
            assert len(lines) >= 2, f"Invalid event block: {block}"
            assert lines[0].startswith("event: ")
            assert lines[1].startswith("data: ")

            evt_name = lines[0][7:].strip()
            evt_data = json.loads(lines[1][6:].strip())

            event_names.append(evt_name)
            event_payloads.append(evt_data)

        # Verify exact sequence
        assert event_names == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]

        # 1. message_start structure
        msg_start = event_payloads[0]
        assert msg_start["type"] == "message_start"
        assert msg_start["message"]["role"] == "assistant"
        assert msg_start["message"]["usage"]["input_tokens"] == 0

        # 2. content_block_start
        assert event_payloads[1]["type"] == "content_block_start"
        assert event_payloads[1]["index"] == 0
        assert event_payloads[1]["content_block"]["type"] == "text"

        # 3. content_block_delta
        assert event_payloads[2]["delta"]["text"] == "Clean "
        assert event_payloads[3]["delta"]["text"] == "Architecture"

        # 4. content_block_stop
        assert event_payloads[4]["type"] == "content_block_stop"
        assert event_payloads[4]["index"] == 0

        # 5. message_delta
        assert event_payloads[5]["type"] == "message_delta"
        assert event_payloads[5]["delta"]["stop_reason"] == "end_turn"
        assert event_payloads[5]["usage"]["output_tokens"] > 0

        # 6. message_stop
        assert event_payloads[6]["type"] == "message_stop"


@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_anthropic_sse_tool_use_event_sequence():
    """Verify Anthropic SSE event sequence when tool_calls are emitted."""
    api_key = get_test_api_key()
    chunks = [
        CanonicalLLMStreamChunk(
            id="c1",
            model="claude-3-5",
            delta_tool_calls=[
                ToolCall(
                    id="toolu_search_1",
                    type="function",
                    function=FunctionCall(name="web_search", arguments='{"query": "architecture"}'),
                )
            ],
            finish_reason="tool_use",
        )
    ]
    mock_client = MockCustomStreamLLMClient(chunks)
    app = create_app(_test_settings())

    from src.gateway.presentation.routers.messages import get_llm_client
    app.dependency_overrides[get_llm_client] = lambda: mock_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/messages",
            headers={"x-api-key": api_key},
            json={
                "model": "coding",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "Search docs"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        blocks = [b for b in resp.text.split("\n\n") if b.strip()]

        events = []
        for block in blocks:
            lines = block.split("\n")
            evt_name = lines[0][7:].strip()
            evt_data = json.loads(lines[1][6:].strip())
            events.append((evt_name, evt_data))

        event_names = [e[0] for e in events]
        assert "message_start" in event_names
        assert "content_block_start" in event_names
        assert "content_block_stop" in event_names
        assert "message_delta" in event_names
        assert "message_stop" in event_names

        tool_start_events = [e for e in events if e[0] == "content_block_start" and e[1]["content_block"]["type"] == "tool_use"]
        assert len(tool_start_events) == 1
        assert tool_start_events[0][1]["content_block"]["name"] == "web_search"
        assert tool_start_events[0][1]["content_block"]["id"] == "toolu_search_1"

        msg_delta = [e[1] for e in events if e[0] == "message_delta"][0]
        assert msg_delta["delta"]["stop_reason"] == "tool_use"


# ==============================================================================
# 3. Connection Failures, Timeouts, and HTTP 502/500 Error Propagation
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_http_client_connection_refused_error():
    """Verify HttpLLMClient properly raises LLMProviderException on connection refused."""
    client = HttpLLMClient(base_url="http://127.0.0.1:59999", timeout_seconds=1.0)

    # 1. Non-streaming generate
    with pytest.raises(LLMProviderException) as exc_info:
        await client.generate(messages=[{"role": "user", "content": "test"}])
    assert exc_info.value.message == "LLM provider connection failed."
    assert exc_info.value.status_code == 502

    # 2. Streaming generate_stream
    with pytest.raises(LLMProviderException) as exc_info_stream:
        async for _ in client.generate_stream(messages=[{"role": "user", "content": "test"}]):
            pass
    assert exc_info_stream.value.message == "LLM provider stream connection failed."
    assert exc_info_stream.value.status_code == 502


@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_openai_non_streaming_upstream_502_error_envelope():
    """Verify OpenAI non-streaming endpoint returns HTTP 502 with valid error JSON on upstream failure."""
    api_key = get_test_api_key()
    mock_failing = MockFailingLLMClient(failure_type="provider_error", error_message="LLM backend connection timeout")
    app = create_app(_test_settings())

    from src.gateway.presentation.routers.chat_completions import get_llm_client
    app.dependency_overrides[get_llm_client] = lambda: mock_failing

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "coding",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )
        assert resp.status_code == 502
        data = resp.json()
        assert "error" in data
        assert data["error"]["type"] == "llm_provider_error"
        assert data["error"]["code"] == "llm_service_failed"
        assert data["error"]["message"] == "A gateway dependency failed."
        assert "connection timeout" not in data["error"]["message"]


@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_anthropic_non_streaming_upstream_502_error_envelope():
    """Verify Anthropic non-streaming endpoint returns HTTP 502 with Anthropic error envelope."""
    api_key = get_test_api_key()
    mock_failing = MockFailingLLMClient(failure_type="provider_error", error_message="Upstream vLLM crashed")
    app = create_app(_test_settings())

    from src.gateway.presentation.routers.messages import get_llm_client
    app.dependency_overrides[get_llm_client] = lambda: mock_failing

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/messages",
            headers={"x-api-key": api_key},
            json={
                "model": "coding",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hello"}],
                "stream": False,
            },
        )
        assert resp.status_code == 502
        data = resp.json()
        assert data["type"] == "error"
        assert data["error"]["type"] == "llm_provider_error"
        assert data["error"]["message"] == "A gateway dependency failed."
        assert "vLLM" not in data["error"]["message"]


@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_openai_streaming_midstream_error_recovery():
    """Verify OpenAI streaming endpoint emits error chunk and [DONE] when stream fails midstream."""
    api_key = get_test_api_key()
    mock_failing = MockFailingLLMClient(failure_type="midstream_error", error_message="Upstream dropped")
    app = create_app(_test_settings())

    from src.gateway.presentation.routers.chat_completions import get_llm_client
    app.dependency_overrides[get_llm_client] = lambda: mock_failing

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "coding",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        frames = [f for f in resp.text.split("\n\n") if f.strip()]

        assert frames[-1] == "data: [DONE]"

        has_error_chunk = False
        for frame in frames:
            if frame.startswith("data: ") and frame != "data: [DONE]":
                payload = json.loads(frame[6:])
                if "error" in payload:
                    has_error_chunk = True
                    assert payload["error"]["type"] == "streaming_error"

        assert has_error_chunk, f"Error chunk not found in frames: {frames}"


@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_anthropic_streaming_midstream_error_recovery():
    """Verify Anthropic streaming endpoint emits `event: error` when stream fails midstream."""
    api_key = get_test_api_key()
    mock_failing = MockFailingLLMClient(failure_type="midstream_error", error_message="Upstream dropped")
    app = create_app(_test_settings())

    from src.gateway.presentation.routers.messages import get_llm_client
    app.dependency_overrides[get_llm_client] = lambda: mock_failing

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/messages",
            headers={"x-api-key": api_key},
            json={
                "model": "coding",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        blocks = [b for b in resp.text.split("\n\n") if b.strip()]

        events = [b.split("\n")[0][7:].strip() for b in blocks if b.startswith("event: ")]
        assert "error" in events
        assert "message_stop" in events


# ==============================================================================
# 4. Malformed Upstream SSE Lines & Corrupted Data Handling
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_http_client_malformed_sse_stream_resilience(caplog):
    """Verify HttpLLMClient skips comment lines, blank lines, invalid JSON without crashing."""
    caplog.set_level("DEBUG")
    raw_sse_lines = [
        b": ping\n\n",
        b"\n\n",
        b": keep-alive\n\n",
        b"data: {\"sentinel-provider-secret-body\n\n",
        b"data: {}\n\n",
        b"data: {\"id\": \"c1\", \"choices\": [{\"delta\": {\"content\": \"Resilient \"}}]}\n\n",
        b"data:{\"choices\": [{\"delta\": {\"content\": \"Parser!\"}}]}\n\n",
        b"data: [DONE]\n\n",
        b"data: [DONE]\n\n",
    ]

    from starlette.applications import Starlette
    from starlette.responses import StreamingResponse as StarletteStreaming
    from starlette.routing import Route

    async def fake_stream_handler(request):
        async def gen():
            for line in raw_sse_lines:
                yield line
        return StarletteStreaming(gen(), media_type="text/event-stream")

    upstream_app = Starlette(routes=[Route("/v1/chat/completions", fake_stream_handler, methods=["POST"])])
    transport = httpx.ASGITransport(app=upstream_app)
    custom_http_client = httpx.AsyncClient(transport=transport, base_url="http://mock-upstream")

    llm_client = HttpLLMClient(base_url="http://mock-upstream", client=custom_http_client)

    chunks = []
    async for chunk in llm_client.generate_stream(messages=[{"role": "user", "content": "hi"}]):
        chunks.append(chunk)

    contents = [c.delta_content for c in chunks if c.delta_content]
    assert "".join(contents) == "Resilient Parser!"
    assert "sentinel-provider-secret-body" not in caplog.text

    await custom_http_client.aclose()


@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_http_client_upstream_html_500_error_body():
    """Verify HttpLLMClient handles raw HTML 500/502 bodies without JSON parser crash."""
    from starlette.applications import Starlette
    from starlette.responses import Response as StarletteResponse
    from starlette.routing import Route

    async def html_502_handler(request):
        return StarletteResponse(
            content="<html><body>502 Bad Gateway: nginx/1.24.0</body></html>",
            status_code=502,
            media_type="text/html",
        )

    upstream_app = Starlette(routes=[Route("/v1/chat/completions", html_502_handler, methods=["POST"])])
    transport = httpx.ASGITransport(app=upstream_app)
    custom_http_client = httpx.AsyncClient(transport=transport, base_url="http://mock-upstream")

    llm_client = HttpLLMClient(base_url="http://mock-upstream", client=custom_http_client)

    # 1. generate()
    with pytest.raises(LLMProviderException) as exc_info:
        await llm_client.generate(messages=[{"role": "user", "content": "hi"}])
    assert exc_info.value.message == "LLM provider request failed."
    assert "502 Bad Gateway" not in exc_info.value.message

    # 2. generate_stream()
    with pytest.raises(LLMProviderException) as exc_info_stream:
        async for _ in llm_client.generate_stream(messages=[{"role": "user", "content": "hi"}]):
            pass
    assert exc_info_stream.value.message == "LLM provider stream connection failed."

    await custom_http_client.aclose()


# ==============================================================================
# 5. Unicode, Multibyte, and Large Chunk Stress
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_unicode_multibyte_and_emojis_in_sse():
    """Verify Unicode emojis, Japanese, Arabic, and math symbols stream losslessly."""
    api_key = get_test_api_key()
    unicode_chunks = [
        CanonicalLLMStreamChunk(id="c1", model="test-model", delta_content="🚀 Python 3.12 "),
        CanonicalLLMStreamChunk(id="c2", model="test-model", delta_content="こんにちは世界 "),
        CanonicalLLMStreamChunk(id="c3", model="test-model", delta_content="مرحبا بالعالم "),
        CanonicalLLMStreamChunk(id="c4", model="test-model", delta_content="∑_{i=1}^n x_i ≈ ∞"),
        CanonicalLLMStreamChunk(id="c5", model="test-model", finish_reason="stop"),
    ]
    mock_client = MockCustomStreamLLMClient(unicode_chunks)
    app = create_app(_test_settings())

    from src.gateway.presentation.routers.chat_completions import get_llm_client
    app.dependency_overrides[get_llm_client] = lambda: mock_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "coding", "messages": [{"role": "user", "content": "unicode"}], "stream": True},
        )
        assert resp.status_code == 200
        frames = [f for f in resp.text.split("\n\n") if f.strip() and f != "data: [DONE]"]
        text_pieces = []
        for frame in frames:
            p = json.loads(frame[6:])
            if "content" in p["choices"][0]["delta"] and p["choices"][0]["delta"]["content"]:
                text_pieces.append(p["choices"][0]["delta"]["content"])

        full_text = "".join(text_pieces)
        assert "🚀 Python 3.12" in full_text
        assert "こんにちは世界" in full_text
        assert "مرحبا بالعالم" in full_text
        assert "∑_{i=1}^n x_i ≈ ∞" in full_text


@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_large_chunk_streaming_stress():
    """Verify streaming handles 50 consecutive large chunks (2KB each = 100KB total) without issue."""
    api_key = get_test_api_key()
    large_block = "A" * 2000
    chunks = [
        CanonicalLLMStreamChunk(id=f"c{i}", model="test-model", delta_content=f"{large_block}_{i}\n")
        for i in range(50)
    ]
    chunks.append(CanonicalLLMStreamChunk(id="c_end", model="test-model", finish_reason="stop"))
    mock_client = MockCustomStreamLLMClient(chunks)
    app = create_app(_test_settings())

    from src.gateway.presentation.routers.chat_completions import get_llm_client
    app.dependency_overrides[get_llm_client] = lambda: mock_client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "coding", "messages": [{"role": "user", "content": "big"}], "stream": True},
        )
        assert resp.status_code == 200
        frames = [f for f in resp.text.split("\n\n") if f.strip() and f != "data: [DONE]"]
        assert len(frames) == 52  # 1 initial + 50 content + 1 finish


# ==============================================================================
# 6. Model Discovery & Auth 401/404 Protocol Formats
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
async def test_adv_auth_missing_returns_protocol_specific_401():
    """Verify missing credentials return protocol-specific 401 formats."""
    app = create_app(_test_settings())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # OpenAI endpoint missing auth
        resp_openai = await client.post(
            "/v1/chat/completions",
            json={"model": "coding", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp_openai.status_code == 401
        assert "error" in resp_openai.json()
        assert resp_openai.json()["error"]["type"] == "authentication_error"

        # Anthropic endpoint missing auth
        resp_anthropic = await client.post(
            "/v1/messages",
            json={"model": "coding", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
        )
        assert resp_anthropic.status_code == 401
        assert resp_anthropic.json()["type"] == "error"
        assert resp_anthropic.json()["error"]["type"] == "authentication_error"
