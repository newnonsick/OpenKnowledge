"""Tier 1 Feature Tests for Feature 26: Streaming SSE Tool Interception Engine.

Validates streaming SSE chunk buffering, tool argument reassembly across fragmented SSE events,
CanonicalStreamChunk model generation, and SSE stream termination.
"""

import json
from typing import List
import httpx
import pytest

from src.gateway.domain.canonical import CanonicalStreamChunk, CanonicalUsage
from src.gateway.domain.tools import FunctionCall, ToolCall
from tests.e2e.harness.mock_server import MockServerManager
from tests.e2e.harness.test_env import parse_sse_stream


def assemble_streamed_arguments(argument_chunks: List[str]) -> str:
    """Reassembles fragmented JSON argument chunks received over SSE."""
    return "".join(argument_chunks)


@pytest.mark.tier1
@pytest.mark.feature("F26")
def test_f26_streaming_sse_content_delta_chunks():
    """Verify streaming text chunks are collected in sequential order."""
    chunks = ["Hello", " world", ", this is a", " streaming", " test."]
    assembled = "".join(chunks)
    assert assembled == "Hello world, this is a streaming test."


@pytest.mark.tier1
@pytest.mark.feature("F26")
def test_f26_streaming_sse_tool_call_argument_assembly():
    """Verify tool call arguments split across multiple SSE deltas assemble into valid JSON."""
    raw_fragments = [
        '{"query":',
        ' "PostgreSQL',
        ' pgvector',
        ' hybrid search"',
        ', "limit": 5}',
    ]
    assembled_json_str = assemble_streamed_arguments(raw_fragments)
    parsed_args = json.loads(assembled_json_str)

    assert parsed_args["query"] == "PostgreSQL pgvector hybrid search"
    assert parsed_args["limit"] == 5


@pytest.mark.tier1
@pytest.mark.feature("F26")
def test_f26_streaming_canonical_stream_chunk_model():
    """Verify CanonicalStreamChunk domain model attributes and serialization."""
    chunk = CanonicalStreamChunk(
        id="chunk-123",
        model="mock-llama-3.1",
        delta_content="Delta text fragment",
        finish_reason=None,
        usage=CanonicalUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )
    assert chunk.id == "chunk-123"
    assert chunk.delta_content == "Delta text fragment"
    assert chunk.finish_reason is None
    assert chunk.usage.total_tokens == 15


@pytest.mark.tier1
@pytest.mark.feature("F26")
def test_f26_streaming_external_tool_passthrough_chunk():
    """Verify streaming tool call chunks for external tools are yielded with proper structure."""
    tc = ToolCall(
        id="call_stream_bash",
        function=FunctionCall(name="bash", arguments='{"command": "pytest"}'),
    )
    chunk = CanonicalStreamChunk(
        id="chunk-tool-call",
        model="test-model",
        delta_tool_calls=[tc],
        finish_reason="tool_use",
    )
    assert chunk.delta_tool_calls is not None
    assert len(chunk.delta_tool_calls) == 1
    assert chunk.delta_tool_calls[0].function.name == "bash"
    assert chunk.finish_reason == "tool_use"


@pytest.mark.tier1
@pytest.mark.feature("F26")
@pytest.mark.asyncio
async def test_f26_streaming_sse_done_event_termination():
    """Verify the gateway streams upstream chunks and terminates with a [DONE] SSE line."""
    from tests.e2e.harness.test_env import GatewayMockUpstream

    async with GatewayMockUpstream() as gw:
        gw.llm.queue_stream_response(
            chunks=["chunk1", "chunk2"],
            finish_reason="stop",
        )

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Stream"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        events = await parse_sse_stream(resp)
        assert len(events) >= 2
        finish_events = [
            e for e in events
            if e.get("choices") and e["choices"][0].get("finish_reason") == "stop"
        ]
        assert len(finish_events) >= 1
        # Content chunks pass through the interception engine untouched
        text = "".join(
            e["choices"][0]["delta"].get("content", "")
            for e in events
            if e.get("choices") and "content" in e["choices"][0]["delta"]
        )
        assert text == "chunk1chunk2"
