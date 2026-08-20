"""Regression tests for thinking / reasoning_content pass-through pipeline.

Covers the reported bug: responses from reasoning-capable LLM backends
(vLLM / llama.cpp / LM Studio returning ``reasoning_content``) dropped the
thinking trace entirely, so clients never received it in either the OpenAI
(``reasoning_content``) or Anthropic (``thinking`` block) protocol.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

import httpx
import pytest

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.application.services.chat_orchestrator import ChatOrchestratorService
from src.gateway.domain.canonical import (
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalThinkingBlock,
    CanonicalUsage,
)
from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient
from src.gateway.presentation.converters.anthropic_converter import (
    anthropic_request_to_canonical,
    canonical_response_to_anthropic,
)
from src.gateway.presentation.converters.openai_converter import (
    canonical_response_to_openai,
    canonical_stream_chunk_to_openai,
)
from src.gateway.presentation.schemas.anthropic_schemas import AnthropicMessagesRequest
from src.gateway.presentation.schemas.openai_schemas import (
    OpenAIChatCompletionRequest,
)


# ==============================================================================
# 1. HttpLLMClient parsing of backend reasoning fields
# ==============================================================================

def _make_llm_client(handler) -> HttpLLMClient:
    transport = httpx.MockTransport(handler)
    return HttpLLMClient(
        base_url="http://mock-llm.test/v1",
        api_key="mock-key",
        client=httpx.AsyncClient(transport=transport),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_llm_client_generate_parses_reasoning_content():
    """Non-streaming backend response message.reasoning_content must be captured."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "chatcmpl-r1",
            "object": "chat.completion",
            "model": "qwen3-test",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "The answer is 42.",
                        "reasoning_content": "Let me think about this step by step.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    client = _make_llm_client(handler)
    resp = await client.generate(messages=[{"role": "user", "content": "hi"}])

    assert resp.reasoning_content == "Let me think about this step by step."
    assert resp.content == "The answer is 42."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_llm_client_generate_parses_reasoning_alias():
    """Backends using the ``reasoning`` key (Ollama-style) must also be handled."""
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "chatcmpl-r2",
            "model": "qwen3-test",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                        "reasoning": "inner monologue",
                    },
                    "finish_reason": "stop",
                }
            ],
        })

    client = _make_llm_client(handler)
    resp = await client.generate(messages=[{"role": "user", "content": "hi"}])
    assert resp.reasoning_content == "inner monologue"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_llm_client_stream_parses_reasoning_deltas():
    """Streaming backend deltas carrying reasoning_content must be captured."""

    def sse(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    body = (
        sse({"id": "c1", "model": "qwen3-test", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "Thinking part 1."}, "finish_reason": None}]})
        + sse({"id": "c1", "model": "qwen3-test", "choices": [{"index": 0, "delta": {"reasoning_content": " part 2."}, "finish_reason": None}]})
        + sse({"id": "c1", "model": "qwen3-test", "choices": [{"index": 0, "delta": {"content": "Final answer."}, "finish_reason": None}]})
        + sse({"id": "c1", "model": "qwen3-test", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        + "data: [DONE]\n\n"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    client = _make_llm_client(handler)
    chunks: List[CanonicalLLMStreamChunk] = []
    async for chunk in client.generate_stream(messages=[{"role": "user", "content": "hi"}]):
        chunks.append(chunk)

    reasoning = "".join(c.delta_reasoning_content or "" for c in chunks)
    content = "".join(c.delta_content or "" for c in chunks)
    assert reasoning == "Thinking part 1. part 2."
    assert content == "Final answer."


# ==============================================================================
# 2. Canonical -> OpenAI conversion
# ==============================================================================

@pytest.mark.unit
def test_openai_nonstream_response_includes_reasoning_content():
    resp = CanonicalChatResponse(
        id="chatcmpl-1",
        model="qwen3-test",
        content=[
            CanonicalThinkingBlock(thinking="Deep thought process."),
            CanonicalTextBlock(text="Visible answer."),
        ],
        finish_reason="stop",
        usage=CanonicalUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
    )
    out = canonical_response_to_openai(resp)
    assert out.choices[0].message.reasoning_content == "Deep thought process."
    assert out.choices[0].message.content == "Visible answer."


@pytest.mark.unit
def test_openai_stream_chunk_includes_reasoning_content():
    chunk = CanonicalStreamChunk(
        id="c1",
        model="qwen3-test",
        delta_thinking="partial thought",
    )
    out = canonical_stream_chunk_to_openai(chunk)
    assert out.choices[0].delta.reasoning_content == "partial thought"


@pytest.mark.unit
def test_openai_request_parses_assistant_reasoning_content():
    """Round-trip: assistant messages with reasoning_content must reach canonical."""
    req = OpenAIChatCompletionRequest.model_validate({
        "model": "qwen3-test",
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "prior thought",
                "tool_calls": [],
            },
            {"role": "user", "content": "and now?"},
        ],
    })
    from src.gateway.presentation.converters.openai_converter import (
        openai_request_to_canonical as _conv,
    )
    canonical_req = _conv(req)
    assistant_msgs = [m for m in canonical_req.messages if m.role == "assistant"]
    assert assistant_msgs, "assistant message must be preserved"
    thinking_blocks = [
        b for b in assistant_msgs[0].content if isinstance(b, CanonicalThinkingBlock)
    ]
    assert thinking_blocks and thinking_blocks[0].thinking == "prior thought"


# ==============================================================================
# 3. Canonical -> Anthropic conversion
# ==============================================================================

@pytest.mark.unit
def test_anthropic_nonstream_response_includes_thinking_block():
    resp = CanonicalChatResponse(
        id="msg_1",
        model="qwen3-test",
        content=[
            CanonicalThinkingBlock(thinking="Chain of thought."),
            CanonicalTextBlock(text="Answer."),
        ],
        finish_reason="stop",
    )
    out = canonical_response_to_anthropic(resp)
    thinking_blocks = [b for b in out.content if isinstance(b, dict) and b.get("type") == "thinking"]
    assert thinking_blocks, "thinking block must be present in Anthropic response"
    assert thinking_blocks[0]["thinking"] == "Chain of thought."
    assert "signature" not in thinking_blocks[0]
    # thinking must come before text
    assert out.content.index(thinking_blocks[0]) == 0


@pytest.mark.unit
def test_anthropic_request_parses_thinking_blocks():
    req = AnthropicMessagesRequest.model_validate({
        "model": "qwen3-test",
        "max_tokens": 100,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "prior thought", "signature": "sig"},
                    {"type": "text", "text": "prior answer"},
                ],
            },
            {"role": "user", "content": "next"},
        ],
    })
    canonical_req = anthropic_request_to_canonical(req)
    assistant_msgs = [m for m in canonical_req.messages if m.role == "assistant"]
    assert assistant_msgs
    thinking_blocks = [
        b for b in assistant_msgs[0].content if isinstance(b, CanonicalThinkingBlock)
    ]
    assert thinking_blocks and thinking_blocks[0].thinking == "prior thought"


# ==============================================================================
# 4. Orchestrator propagation
# ==============================================================================

class ReasoningMockLLMClient(ILLMClient):
    """Mock upstream client emitting reasoning content in both modes."""

    async def generate(self, messages, tools=None, model=None, temperature=None,
                       max_tokens=None, **kwargs):
        return CanonicalLLMResponse(
            id="chatcmpl-reasoning",
            model=model or "qwen3-test",
            content="Visible.",
            reasoning_content="Hidden reasoning.",
            finish_reason="stop",
        )

    async def generate_stream(self, messages, tools=None, model=None, temperature=None,
                              max_tokens=None, **kwargs):
        yield CanonicalLLMStreamChunk(
            id="c1", model=model or "qwen3-test",
            delta_reasoning_content="Hidden ",
        )
        yield CanonicalLLMStreamChunk(
            id="c1", model=model or "qwen3-test",
            delta_reasoning_content="reasoning.",
        )
        yield CanonicalLLMStreamChunk(
            id="c1", model=model or "qwen3-test",
            delta_content="Visible.",
        )
        yield CanonicalLLMStreamChunk(
            id="c1", model=model or "qwen3-test",
            finish_reason="stop",
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_nonstream_propagates_thinking():
    orchestrator = ChatOrchestratorService(llm_client=ReasoningMockLLMClient())
    req = CanonicalChatRequest(
        model="qwen3-test",
        messages=[CanonicalMessage(role="user", content="hi")],
    )
    resp = await orchestrator.orchestrate_chat(req)
    thinking_blocks = [b for b in resp.content if isinstance(b, CanonicalThinkingBlock)]
    assert thinking_blocks and thinking_blocks[0].thinking == "Hidden reasoning."
    text_blocks = [b for b in resp.content if isinstance(b, CanonicalTextBlock)]
    assert text_blocks and text_blocks[0].text == "Visible."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_stream_propagates_thinking():
    orchestrator = ChatOrchestratorService(llm_client=ReasoningMockLLMClient())
    req = CanonicalChatRequest(
        model="qwen3-test",
        messages=[CanonicalMessage(role="user", content="hi")],
        stream=True,
    )
    thinking_parts: List[str] = []
    content_parts: List[str] = []
    async for chunk in orchestrator.orchestrate_chat_stream(req):
        if chunk.delta_thinking:
            thinking_parts.append(chunk.delta_thinking)
        if chunk.delta_content:
            content_parts.append(chunk.delta_content)

    assert "".join(thinking_parts) == "Hidden reasoning."
    assert "".join(content_parts) == "Visible."


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_sends_reasoning_upstream_in_tool_loop():
    """Assistant reasoning must be replayed upstream as reasoning_content during tool loops."""
    from src.gateway.domain.tools import FunctionCall, ToolCall

    class SequencedClient(ReasoningMockLLMClient):
        def __init__(self):
            self.captured_messages: List[dict] = []

        async def generate(self, messages, tools=None, model=None, temperature=None,
                           max_tokens=None, **kwargs):
            self.captured_messages.append(messages)
            if len(self.captured_messages) == 1:
                return CanonicalLLMResponse(
                    id="chatcmpl-tool",
                    model=model or "qwen3-test",
                    content=None,
                    reasoning_content="I should save this.",
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            function=FunctionCall(
                                name="knowledge_save",
                                arguments=json.dumps({
                                    "title": "T", "content": "C",
                                }),
                            ),
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return await super().generate(messages, tools, model, temperature, max_tokens, **kwargs)

    class NoopKnowledge:
        async def execute_tool(self, tool_call_id, name, arguments, session_workspace_id=None):
            from src.gateway.domain.tools import ToolResult
            return ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                content=json.dumps({"status": "created", "id": "x"}),
                is_error=False,
            )

    client = SequencedClient()
    orchestrator = ChatOrchestratorService(llm_client=client, knowledge_service=NoopKnowledge())
    req = CanonicalChatRequest(
        model="qwen3-test",
        messages=[CanonicalMessage(role="user", content="remember this")],
    )
    await orchestrator.orchestrate_chat(req)

    second_round = client.captured_messages[1]
    assistant_round = [m for m in second_round if m["role"] == "assistant"]
    assert assistant_round, "assistant tool-call round must be replayed upstream"
    assert assistant_round[0].get("reasoning_content") == "I should save this."
