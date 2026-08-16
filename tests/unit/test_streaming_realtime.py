"""Unit tests for Real-Time Streaming and Parameter Forwarding."""

from typing import Any, AsyncIterator, Dict, List, Optional
import pytest

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.application.services.chat_orchestrator import ChatOrchestratorService
from src.gateway.domain.canonical import (
    CanonicalChatRequest,
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalMessage,
    CanonicalTextBlock,
    CanonicalUsage,
)
from src.gateway.domain.tools import FunctionCall, ToolCall


class MockStreamingLLMClient(ILLMClient):
    """Mock LLM client tracking real-time generate_stream calls."""

    def __init__(self, chunks: List[CanonicalLLMStreamChunk]):
        self.chunks = chunks
        self.received_kwargs: Dict[str, Any] = {}

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> CanonicalLLMResponse:
        self.received_kwargs = kwargs
        return CanonicalLLMResponse(
            id="mock-resp",
            model=model or "default",
            content="non-streaming response",
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
        self.received_kwargs = kwargs
        for c in self.chunks:
            yield c


@pytest.mark.asyncio
async def test_realtime_text_streaming_yields_immediately():
    """Verify that pure text stream chunks are yielded in real-time without buffering."""
    stream_chunks = [
        CanonicalLLMStreamChunk(id="c1", model="test-model", delta_content="Hello"),
        CanonicalLLMStreamChunk(id="c2", model="test-model", delta_content=" "),
        CanonicalLLMStreamChunk(id="c3", model="test-model", delta_content="world"),
        CanonicalLLMStreamChunk(id="c4", model="test-model", delta_content="!", finish_reason="stop"),
    ]
    mock_client = MockStreamingLLMClient(chunks=stream_chunks)
    orchestrator = ChatOrchestratorService(llm_client=mock_client)

    request = CanonicalChatRequest(
        model="test-model",
        messages=[
            CanonicalMessage(role="user", content=[CanonicalTextBlock(text="Say hello")]),
        ],
        stream=True,
        extra_params={"presence_penalty": 0.5},
    )

    yielded_chunks = []
    async for chunk in orchestrator.orchestrate_chat_stream(request):
        yielded_chunks.append(chunk)

    # Verify each chunk was yielded with correct delta_content
    assert len(yielded_chunks) == 4
    assert [c.delta_content for c in yielded_chunks] == ["Hello", " ", "world", "!"]
    assert yielded_chunks[-1].finish_reason == "stop"
    # Verify extra_params was forwarded to llm_client
    assert mock_client.received_kwargs.get("presence_penalty") == 0.5


@pytest.mark.asyncio
async def test_external_tool_streaming_yields_immediately():
    """Verify that external tool calls are yielded to the client in real-time."""
    tc = ToolCall(
        id="call_bash_1",
        function=FunctionCall(name="bash", arguments='{"command": "echo 1"}'),
    )
    stream_chunks = [
        CanonicalLLMStreamChunk(id="c1", model="test-model", delta_tool_calls=[tc], finish_reason="tool_calls"),
    ]
    mock_client = MockStreamingLLMClient(chunks=stream_chunks)
    orchestrator = ChatOrchestratorService(llm_client=mock_client)

    request = CanonicalChatRequest(
        model="test-model",
        messages=[
            CanonicalMessage(role="user", content=[CanonicalTextBlock(text="Run bash")]),
        ],
        stream=True,
    )

    yielded_chunks = []
    async for chunk in orchestrator.orchestrate_chat_stream(request):
        yielded_chunks.append(chunk)

    assert len(yielded_chunks) == 1
    assert yielded_chunks[0].delta_tool_calls is not None
    assert yielded_chunks[0].delta_tool_calls[0].function.name == "bash"
    assert yielded_chunks[0].finish_reason == "tool_use"
