"""Unit tests for ChatOrchestratorService non-streaming and streaming SSE orchestration."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.application.services.chat_orchestrator import (
    ChatOrchestratorService,
    IChatOrchestrator,
)
from src.gateway.application.services.knowledge_service import KnowledgeService
from src.gateway.domain.canonical import (
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
)
from src.gateway.domain.tools import (
    FunctionCall,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from src.gateway.domain.exceptions import ToolExecutionException


class MockStreamingLLMClient(ILLMClient):
    """Mock LLM client supporting both non-streaming and streaming generator queues."""

    def __init__(self) -> None:
        self.non_streaming_queue: List[CanonicalLLMResponse] = []
        self.stream_queues: List[List[CanonicalLLMStreamChunk]] = []
        self.call_count: int = 0
        self.stream_call_count: int = 0

    def queue_response(self, resp: CanonicalLLMResponse) -> None:
        self.non_streaming_queue.append(resp)

    def queue_stream_chunks(self, chunks: List[CanonicalLLMStreamChunk]) -> None:
        self.stream_queues.append(chunks)

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> CanonicalLLMResponse:
        self.call_count += 1
        if self.non_streaming_queue:
            return self.non_streaming_queue.pop(0)
        return CanonicalLLMResponse(
            id="chatcmpl-default",
            model=model or "test-model",
            content="Mock generate output",
            finish_reason="stop",
        )

    async def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[CanonicalLLMStreamChunk]:
        self.stream_call_count += 1
        if self.stream_queues:
            chunks = self.stream_queues.pop(0)
            for chunk in chunks:
                yield chunk
        else:
            yield CanonicalLLMStreamChunk(
                id="chunk-default",
                model=model or "test-model",
                delta_content="Default stream content",
                finish_reason="stop",
            )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_non_streaming_direct_text():
    """Verify non-streaming direct text response without tool calls."""
    client = MockStreamingLLMClient()
    client.queue_response(
        CanonicalLLMResponse(
            id="chatcmpl-direct-1",
            model="gpt-4o",
            content="Hello! I am a helpful AI assistant.",
            finish_reason="stop",
            usage=CanonicalUsage(prompt_tokens=10, completion_tokens=8, total_tokens=18),
        )
    )

    orchestrator = ChatOrchestratorService(llm_client=client)
    req = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="Hello")],
    )

    resp = await orchestrator.orchestrate_chat(req)

    assert resp.id == "chatcmpl-direct-1"
    assert resp.model == "gpt-4o"
    assert resp.finish_reason == "stop"
    assert len(resp.content) == 1
    assert resp.content[0].text == "Hello! I am a helpful AI assistant."
    assert resp.usage.total_tokens == 18
    assert client.call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_streaming_pure_text():
    """Verify streaming SSE pure text chunks are passed through cleanly in order."""
    client = MockStreamingLLMClient()
    client.queue_stream_chunks([
        CanonicalLLMStreamChunk(id="c1", model="gpt-4o", delta_content="Hello"),
        CanonicalLLMStreamChunk(id="c2", model="gpt-4o", delta_content=" world"),
        CanonicalLLMStreamChunk(id="c3", model="gpt-4o", delta_content="!", finish_reason="stop"),
    ])

    orchestrator = ChatOrchestratorService(llm_client=client)
    req = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="Stream me")],
        stream=True,
    )

    chunks: List[CanonicalStreamChunk] = []
    async for chunk in orchestrator.orchestrate_chat_stream(req):
        chunks.append(chunk)

    assert len(chunks) == 3
    full_text = "".join(c.delta_content for c in chunks if c.delta_content)
    assert full_text == "Hello world!"
    assert chunks[-1].finish_reason == "stop"
    assert client.stream_call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_streaming_external_tool_passthrough():
    """Verify streaming external tool call (e.g. bash) is streamed directly with delta_tool_calls."""
    client = MockStreamingLLMClient()
    client.queue_stream_chunks([
        CanonicalLLMStreamChunk(
            id="c_tool",
            model="gpt-4o",
            delta_tool_calls=[
                ToolCall(
                    id="call_bash_stream",
                    function=FunctionCall(name="bash", arguments='{"command": "pytest"}'),
                )
            ],
            finish_reason="tool_calls",
        )
    ])

    orchestrator = ChatOrchestratorService(llm_client=client)
    req = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="Run pytest")],
        stream=True,
    )

    chunks: List[CanonicalStreamChunk] = []
    async for chunk in orchestrator.orchestrate_chat_stream(req):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0].delta_tool_calls is not None
    assert len(chunks[0].delta_tool_calls) == 1
    assert chunks[0].delta_tool_calls[0].function.name == "bash"
    assert chunks[0].finish_reason == "tool_use"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_streaming_internal_tool_interception():
    """Verify streaming internal tool interception: turn 1 (internal tool) is suppressed and executed, turn 2 (final answer) is streamed."""
    mock_ks = MagicMock(spec=KnowledgeService)
    mock_ks.execute_tool = AsyncMock(return_value=ToolResult(
        tool_call_id="call_k_stream_1",
        name="knowledge_search",
        content=json.dumps({"results": [{"title": "Doc", "content": "FastAPI SSE"}], "count": 1}),
        is_error=False,
    ))

    client = MockStreamingLLMClient()

    # Turn 1 Stream: Internal knowledge_search tool call deltas
    client.queue_stream_chunks([
        CanonicalLLMStreamChunk(
            id="t1_c1",
            model="gpt-4o",
            delta_tool_calls=[
                ToolCall(
                    id="call_k_stream_1",
                    function=FunctionCall(name="knowledge_search", arguments='{"query": "Fast'),
                )
            ],
            finish_reason=None,
        ),
        CanonicalLLMStreamChunk(
            id="t1_c2",
            model="gpt-4o",
            delta_tool_calls=[
                ToolCall(
                    id="call_k_stream_1",
                    function=FunctionCall(name="", arguments='API"}'),
                )
            ],
            finish_reason="tool_calls",
        ),
    ])

    # Turn 2 Stream: Final text answer
    client.queue_stream_chunks([
        CanonicalLLMStreamChunk(id="t2_c1", model="gpt-4o", delta_content="FastAPI "),
        CanonicalLLMStreamChunk(id="t2_c2", model="gpt-4o", delta_content="supports SSE streaming."),
        CanonicalLLMStreamChunk(id="t2_c3", model="gpt-4o", delta_content="", finish_reason="stop"),
    ])

    orchestrator = ChatOrchestratorService(
        llm_client=client,
        knowledge_service=mock_ks,
    )

    req = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="How does FastAPI do SSE?")],
        stream=True,
    )

    chunks: List[CanonicalStreamChunk] = []
    async for chunk in orchestrator.orchestrate_chat_stream(req):
        chunks.append(chunk)

    # External client should receive ONLY turn 2 text chunks!
    assert len(chunks) == 3
    full_text = "".join(c.delta_content for c in chunks if c.delta_content)
    assert full_text == "FastAPI supports SSE streaming."
    assert chunks[-1].finish_reason == "stop"
    assert client.stream_call_count == 2
    mock_ks.execute_tool.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_streaming_max_iterations_guardrail():
    mock_ks = MagicMock(spec=KnowledgeService)
    mock_ks.execute_tool = AsyncMock(return_value=ToolResult(
        tool_call_id="call_loop",
        name="knowledge_search",
        content="No data",
        is_error=False,
    ))

    client = MockStreamingLLMClient()
    # Queue 5 turns of internal tool calls with max_tool_iterations=2
    for i in range(5):
        client.queue_stream_chunks([
            CanonicalLLMStreamChunk(
                id=f"c_{i}",
                model="gpt-4o",
                delta_tool_calls=[
                    ToolCall(
                        id=f"call_{i}",
                        function=FunctionCall(name="knowledge_search", arguments=f'{{"query": "{i}"}}'),
                    )
                ],
                finish_reason="tool_calls",
            )
        ])

    orchestrator = ChatOrchestratorService(
        llm_client=client,
        knowledge_service=mock_ks,
        max_tool_iterations=2,
    )

    req = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="Infinite loop stream")],
        stream=True,
    )

    with pytest.raises(ToolExecutionException):
        async for _ in orchestrator.orchestrate_chat_stream(req):
            pass
    assert client.stream_call_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_usage_aggregation_across_turns():
    """Verify token usage prompt_tokens and completion_tokens accumulate across multi-turn tool loops."""
    mock_ks = MagicMock(spec=KnowledgeService)
    mock_ks.execute_tool = AsyncMock(return_value=ToolResult(
        tool_call_id="call_1",
        name="knowledge_search",
        content="Result",
        is_error=False,
    ))

    resp1 = CanonicalLLMResponse(
        id="resp_1",
        model="gpt-4o",
        tool_calls=[
            ToolCall(
                id="call_1",
                function=FunctionCall(name="knowledge_search", arguments='{"query": "test"}'),
            )
        ],
        finish_reason="tool_calls",
        usage=CanonicalUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
    )
    resp2 = CanonicalLLMResponse(
        id="resp_2",
        model="gpt-4o",
        content="Final answer.",
        finish_reason="stop",
        usage=CanonicalUsage(prompt_tokens=40, completion_tokens=15, total_tokens=55),
    )

    client = MockStreamingLLMClient()
    client.queue_response(resp1)
    client.queue_response(resp2)

    orchestrator = ChatOrchestratorService(
        llm_client=client,
        knowledge_service=mock_ks,
    )

    req = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="Test prompt")],
    )

    resp = await orchestrator.orchestrate_chat(req)

    assert resp.usage.prompt_tokens == 60
    assert resp.usage.completion_tokens == 25
    assert resp.usage.total_tokens == 85
