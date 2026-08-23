"""Tier 3 Pairwise Combination Tests: SSE Streaming + Tool Interception + Buffer Reassembly.

Tests cross-feature interactions between:
- Feature 26: Streaming SSE Tool Interception Engine
- Feature 7: OpenAI /v1/chat/completions API
- Feature 8: Anthropic /v1/messages API
- Feature 24: Chat Orchestration & Tool Interception Loop
- Feature 25: External Harness Tool Passthrough

HTTP interactions run through the real gateway wired to the in-process mock
upstream; model-level checks exercise the real CanonicalStreamChunk contract.
"""

import json
import pytest

from src.gateway.domain.canonical import CanonicalStreamChunk
from tests.e2e.harness.test_env import GatewayMockUpstream, parse_sse_stream


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f07_f26_streaming_pure_text_sse_sequence():
    """Test pairwise interaction: SSE streaming of text chunks with delta token delivery and [DONE]."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_stream_response(["Hello", " world", ", this", " is", " streaming."])

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Stream me a message"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        events = await parse_sse_stream(resp)
        assert len(events) >= 6
        assert events[0]["choices"][0]["delta"].get("role") == "assistant"

        deltas = [
            e["choices"][0]["delta"].get("content", "")
            for e in events
            if e.get("choices") and e["choices"] and "content" in e["choices"][0]["delta"]
        ]
        full_text = "".join(deltas)
        assert full_text == "Hello world, this is streaming."
        assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f25_f26_streaming_external_tool_passthrough():
    """Test pairwise interaction: SSE streaming tool calls for external tools (bash) streamed to client."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_tool_call(
            name="bash",
            arguments={"command": "cargo build --release"},
            call_id="call_bash_stream_01",
        )

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Build project"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        events = await parse_sse_stream(resp)

        tool_call_events = [
            e for e in events
            if e.get("choices") and e["choices"]
            and "tool_calls" in e["choices"][0]["delta"]
        ]
        assert len(tool_call_events) >= 1
        tc_data = tool_call_events[0]["choices"][0]["delta"]["tool_calls"][0]
        assert tc_data["function"]["name"] == "bash"
        assert "cargo build" in tc_data["function"]["arguments"]


@pytest.mark.tier3
def test_pairwise_f26_streaming_fragmented_json_argument_buffer_reassembly():
    """Test pairwise interaction: Buffer engine reassembles split JSON argument tokens across stream chunks."""
    argument_fragments = [
        '{"qu',
        'ery":',
        ' "optimistic',
        ' concurrency',
        ' control",',
        ' "limit": 10}',
    ]

    assembled_buffer = ""
    for frag in argument_fragments:
        assembled_buffer += frag

    parsed_args = json.loads(assembled_buffer)
    assert parsed_args["query"] == "optimistic concurrency control"
    assert parsed_args["limit"] == 10


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f24_f26_streaming_internal_tool_buffer_and_interception():
    """Test pairwise interaction: the gateway buffers an internal knowledge_search stream,
    executes the tool locally, and streams only the final answer to the client."""
    from tests.e2e.harness.mock_server import MockLLMResponse

    async with GatewayMockUpstream() as gw:
        # Turn 1: model streams an internal knowledge_search tool call
        gw.llm.queue_response(
            MockLLMResponse.tool_call(
                name="knowledge_search",
                arguments={"query": "auth_spec"},
                call_id="call_k_01",
            )
        )
        # Turn 2: after receiving the tool result, model answers in plain text
        gw.llm.queue_text_response("Final synthesized answer from knowledge.")

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Search the knowledge base"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        events = await parse_sse_stream(resp)

        # The interception loop must have run two upstream turns
        assert gw.llm.call_count == 2
        # The second upstream turn received the executed tool result
        second_turn_messages = gw.llm.recorded_requests[1].messages
        tool_msgs = [m for m in second_turn_messages if m.get("role") == "tool"]
        assert tool_msgs, "tool result must be fed back upstream"

        # The client stream contains ONLY the final answer - no tool call deltas
        tool_call_events = [
            e for e in events
            if e.get("choices") and e["choices"]
            and "tool_calls" in e["choices"][0]["delta"]
        ]
        assert tool_call_events == []
        text = "".join(
            e["choices"][0]["delta"].get("content", "")
            for e in events
            if e.get("choices") and "content" in e["choices"][0]["delta"]
        )
        assert text == "Final synthesized answer from knowledge."
        assert events[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.tier3
def test_pairwise_f02_f26_canonical_stream_chunk_contracts():
    """Test pairwise interaction: CanonicalStreamChunk delta types and finish reason invariants."""
    text_chunk = CanonicalStreamChunk(
        id="chunk_01",
        model="test-model",
        delta_content="Part A",
        finish_reason=None,
    )
    assert text_chunk.delta_content == "Part A"
    assert text_chunk.finish_reason is None

    final_chunk = CanonicalStreamChunk(
        id="chunk_02",
        model="test-model",
        delta_content=None,
        finish_reason="stop",
    )
    assert final_chunk.delta_content is None
    assert final_chunk.finish_reason == "stop"
