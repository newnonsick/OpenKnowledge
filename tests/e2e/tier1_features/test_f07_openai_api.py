"""Tier 1 Feature Tests for Feature 7: OpenAI /v1/chat/completions API.

Validates standard non-streaming JSON chat completions, SSE streaming responses,
parameter forwarding (temperature, max_tokens), conversation context, and error payloads.

All HTTP tests exercise the real gateway stack (auth middleware, OpenAI converter,
orchestrator, HTTP LLM adapter) wired to the in-process mock upstream.
"""

import httpx
import pytest

from tests.e2e.harness.test_env import GatewayMockUpstream, parse_sse_stream


@pytest.mark.tier1
@pytest.mark.feature("F7")
@pytest.mark.asyncio
async def test_f07_openai_chat_completions_non_streaming_json():
    """Verify standard non-streaming OpenAI chat completions response structure."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response(
            "Clean Architecture creates decoupled, maintainable software systems.",
            finish_reason="stop",
        )

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "meta-llama/Llama-3.1-8B-Instruct",
                "messages": [{"role": "user", "content": "What is Clean Architecture?"}],
                "stream": False,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert len(data["choices"]) == 1
        choice = data["choices"][0]
        assert choice["message"]["role"] == "assistant"
        assert "Clean Architecture" in choice["message"]["content"]
        assert choice["finish_reason"] == "stop"
        assert "usage" in data
        assert data["usage"]["prompt_tokens"] > 0


@pytest.mark.tier1
@pytest.mark.feature("F7")
@pytest.mark.asyncio
async def test_f07_openai_chat_completions_streaming_sse():
    """Verify Server-Sent Events (SSE) streaming chat completion chunks and termination."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_stream_response(
            chunks=["First chunk, ", "second chunk, ", "final chunk."],
            finish_reason="stop",
        )

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "meta-llama/Llama-3.1-8B-Instruct",
                "messages": [{"role": "user", "content": "Stream response"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        events = await parse_sse_stream(resp)
        assert len(events) >= 3

        # Verify initial delta has assistant role
        first_delta = events[0]["choices"][0]["delta"]
        assert first_delta.get("role") == "assistant"

        # Verify content deltas
        text_chunks = [
            e["choices"][0]["delta"].get("content", "")
            for e in events
            if e["choices"] and "content" in e["choices"][0]["delta"]
        ]
        reconstructed = "".join(text_chunks)
        assert reconstructed == "First chunk, second chunk, final chunk."


@pytest.mark.tier1
@pytest.mark.feature("F7")
@pytest.mark.asyncio
async def test_f07_openai_temperature_and_max_tokens():
    """Verify sampling parameters are forwarded through the gateway to the backend."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response("Sampling parameter test complete.")

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4-custom",
                "messages": [{"role": "user", "content": "Test params"}],
                "temperature": 0.2,
                "max_tokens": 100,
            },
        )
        assert resp.status_code == 200
        assert gw.llm.last_request is not None
        assert gw.llm.last_request.body.get("temperature") == 0.2
        assert gw.llm.last_request.body.get("max_tokens") == 100


@pytest.mark.tier1
@pytest.mark.feature("F7")
@pytest.mark.asyncio
async def test_f07_openai_multi_turn_conversation_context():
    """Verify multi-message conversation history (system, user, assistant, user)."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response("The sum of 2 and 2 is 4.")

        messages = [
            {"role": "system", "content": "You are a precise arithmetic calculator."},
            {"role": "user", "content": "What is 2 + 2?"},
            {"role": "assistant", "content": "2 + 2 equals 4."},
            {"role": "user", "content": "Can you confirm again?"},
        ]

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={"model": "default", "messages": messages},
        )
        assert resp.status_code == 200
        assert gw.llm.last_request is not None
        assert len(gw.llm.last_request.messages) == 4
        assert gw.llm.last_request.get_last_user_message() == "Can you confirm again?"


@pytest.mark.tier1
@pytest.mark.feature("F7")
@pytest.mark.asyncio
async def test_f07_openai_error_response_structure():
    """Verify the gateway error envelope when the upstream LLM fails (e.g. 429).

    Upstream failures surface as a 502 llm_provider_error envelope carrying the
    upstream error message (established gateway contract, see tier 5 tests).
    """
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_error(status_code=429, message="Rate limit exceeded. Please back off.")

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={"model": "default", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == 502
        data = resp.json()
        assert "error" in data
        assert data["error"]["type"] == "llm_provider_error"
        assert "Rate limit" in data["error"]["message"]
