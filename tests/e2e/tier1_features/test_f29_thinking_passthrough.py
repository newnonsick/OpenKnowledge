"""Tier 1 Feature Tests for Thinking / Reasoning Pass-Through (F29).

End-to-end coverage for the reported bug where responses from reasoning-capable
backends (Qwen3 on llama.cpp / LM Studio / vLLM emitting ``reasoning_content``)
silently dropped the thinking trace. The gateway must expose it as:

- OpenAI protocol: ``message.reasoning_content`` / ``delta.reasoning_content``
- Anthropic protocol: ``thinking`` content blocks / ``thinking_delta`` SSE events
"""

import json

import httpx
import pytest

from tests.e2e.harness.mock_server import MockLLMResponse, MockServerManager
from tests.e2e.harness.test_env import TestEnvironment, parse_sse_stream


@pytest.fixture
async def reasoning_env():
    """TestEnvironment with the gateway wired to a live mock LLM emitting reasoning."""
    mock_mgr = MockServerManager()
    await mock_mgr.start()
    try:
        env = TestEnvironment(
            env_overrides={
                "LLM_URL": mock_mgr.llm_url,
                "EMBEDDING_URL": mock_mgr.embedding_url,
            }
        )
        async with env:
            yield env, mock_mgr
    finally:
        await mock_mgr.stop()


@pytest.mark.tier1
@pytest.mark.feature("F29")
@pytest.mark.asyncio
async def test_f29_openai_nonstream_reasoning_content(reasoning_env):
    env, mock_mgr = reasoning_env
    mock_mgr.llm.queue_response(
        MockLLMResponse(
            content="The user's name is New.",
            reasoning_content="The knowledge base says the user is named New.",
            finish_reason="stop",
        )
    )

    async with env.get_client() as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "What is my name?"}],
                "stream": False,
            },
        )
    assert resp.status_code == 200
    message = resp.json()["choices"][0]["message"]
    assert message["content"] == "The user's name is New."
    assert message.get("reasoning_content") == "The knowledge base says the user is named New."


@pytest.mark.tier1
@pytest.mark.feature("F29")
@pytest.mark.asyncio
async def test_f29_openai_stream_reasoning_deltas(reasoning_env):
    env, mock_mgr = reasoning_env
    mock_mgr.llm.queue_response(
        MockLLMResponse.stream(
            chunks=["Answer ", "part two."],
            finish_reason="stop",
            reasoning_content="Thinking it through.",
        )
    )

    async with env.get_client() as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Stream with thoughts"}],
                "stream": True,
            },
        )
    assert resp.status_code == 200
    events = await parse_sse_stream(resp)

    reasoning_parts = [
        e["choices"][0]["delta"]["reasoning_content"]
        for e in events
        if "reasoning_content" in e.get("choices", [{}])[0].get("delta", {})
    ]
    content_parts = [
        e["choices"][0]["delta"]["content"]
        for e in events
        if "content" in e.get("choices", [{}])[0].get("delta", {})
    ]
    assert "".join(reasoning_parts) == "Thinking it through."
    assert "".join(content_parts) == "Answer part two."


@pytest.mark.tier1
@pytest.mark.feature("F29")
@pytest.mark.asyncio
async def test_f29_anthropic_nonstream_thinking_block(reasoning_env):
    env, mock_mgr = reasoning_env
    mock_mgr.llm.queue_response(
        MockLLMResponse(
            content="Visible answer.",
            reasoning_content="Hidden reasoning trace.",
            finish_reason="stop",
        )
    )

    async with env.get_client() as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "Think and answer"}],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    block_types = [b.get("type") for b in data["content"]]
    assert "thinking" in block_types
    thinking_block = next(b for b in data["content"] if b.get("type") == "thinking")
    assert thinking_block["thinking"] == "Hidden reasoning trace."
    assert thinking_block.get("signature") is None
    # thinking must precede the text block
    assert block_types.index("thinking") < block_types.index("text")


@pytest.mark.tier1
@pytest.mark.feature("F29")
@pytest.mark.asyncio
async def test_f29_anthropic_stream_thinking_delta_events(reasoning_env):
    env, mock_mgr = reasoning_env
    mock_mgr.llm.queue_response(
        MockLLMResponse.stream(
            chunks=["Visible ", "stream."],
            finish_reason="stop",
            reasoning_content="Streaming thoughts.",
        )
    )

    async with env.get_client() as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "default",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "Stream with thoughts"}],
                "stream": True,
            },
        )
    assert resp.status_code == 200

    events: list = []
    raw_events: list = []
    async for line in resp.aiter_lines():
        line = line.strip()
        if line.startswith("event: "):
            raw_events.append(line[len("event: "):])
        if line.startswith("data: "):
            payload = line[len("data: "):]
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass

    # Anthropic SSE sequence must contain a thinking block with thinking_delta events
    thinking_deltas = [
        e for e in events
        if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "thinking_delta"
    ]
    assert thinking_deltas, "thinking_delta SSE events must be emitted"
    joined = "".join(e["delta"]["thinking"] for e in thinking_deltas)
    assert joined == "Streaming thoughts."

    # A thinking content_block_start must appear before the text content_block_start
    block_starts = [
        e for e in events if e.get("type") == "content_block_start"
    ]
    thinking_start_idx = next(
        i for i, e in enumerate(block_starts)
        if e["content_block"].get("type") == "thinking"
    )
    text_start_idx = next(
        i for i, e in enumerate(block_starts)
        if e["content_block"].get("type") == "text"
    )
    assert thinking_start_idx < text_start_idx

    # text content must still stream through text_delta events
    text_deltas = [
        e for e in events
        if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta"
    ]
    assert "".join(e["delta"]["text"] for e in text_deltas) == "Visible stream."


@pytest.mark.tier1
@pytest.mark.feature("F29")
@pytest.mark.asyncio
async def test_f29_openai_stream_without_reasoning_content(reasoning_env):
    env, mock_mgr = reasoning_env
    mock_mgr.llm.queue_response(
        MockLLMResponse.stream(
            chunks=["Plain ", "answer."],
            finish_reason="stop",
        )
    )

    async with env.get_client() as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Stream without thoughts"}],
                "stream": True,
            },
        )
    assert resp.status_code == 200
    events = await parse_sse_stream(resp)

    reasoning_parts = [
        e["choices"][0]["delta"]["reasoning_content"]
        for e in events
        if "reasoning_content" in e.get("choices", [{}])[0].get("delta", {})
    ]
    content_parts = [
        e["choices"][0]["delta"]["content"]
        for e in events
        if "content" in e.get("choices", [{}])[0].get("delta", {})
    ]
    assert reasoning_parts == []
    assert "".join(content_parts) == "Plain answer."
