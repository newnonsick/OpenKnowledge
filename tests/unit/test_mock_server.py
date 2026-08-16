"""
Unit / Self-tests for Mock HTTP Server Harness.

Tests:
1. DeterministicEmbeddingEngine (normalization, reproducibility, dimension scaling)
2. MockLLMController & MockEmbeddingController (queueing, rules, request recording)
3. ASGI endpoints (OpenAI completions, SSE streaming, Anthropic format, embeddings, errors)
4. Ephemeral Port Server Manager (lifecycle, real network socket binding, graceful shutdown)
"""

from __future__ import annotations

import asyncio
import json
import math
import pytest
import httpx

from tests.e2e.harness.mock_server import (
    DeterministicEmbeddingEngine,
    MockEmbeddingController,
    MockLLMController,
    MockLLMResponse,
    MockServerManager,
    MockToolCall,
    RecordedRequest,
    create_mock_upstream_app,
)
from tests.e2e.harness.test_env import parse_sse_stream


# ==============================================================================
# 1. Deterministic Embedding Engine Unit Tests
# ==============================================================================

def test_deterministic_embedding_engine_unit_norm():
    """Verify vectors have L2 norm = 1.0."""
    engine = DeterministicEmbeddingEngine(dimension=384)
    texts = [
        "Hello world",
        "Pragmatic Clean Architecture",
        "PostgreSQL pgvector hybrid retrieval",
        "Short",
        "A" * 500,
    ]
    for t in texts:
        vec = engine.generate_vector(t)
        assert len(vec) == 384
        norm = math.sqrt(sum(x * x for x in vec))
        assert pytest.approx(norm, rel=1e-4) == 1.0


def test_deterministic_embedding_engine_reproducibility():
    """Verify identical text produces identical vectors, different text produces different vectors."""
    engine = DeterministicEmbeddingEngine(dimension=128)
    v1 = engine.generate_vector("Database indexing with GIN and HNSW")
    v2 = engine.generate_vector("Database indexing with GIN and HNSW")
    v3 = engine.generate_vector("Completely different search query")

    assert v1 == v2
    assert v1 != v3


def test_deterministic_embedding_engine_dimension_switch():
    """Verify changing dimension works cleanly."""
    engine = DeterministicEmbeddingEngine(dimension=1024)
    v1 = engine.generate_vector("Test query")
    assert len(v1) == 1024

    engine.set_dimension(512)
    v2 = engine.generate_vector("Test query")
    assert len(v2) == 512


def test_deterministic_embedding_engine_custom_vector():
    """Verify custom registered vector override."""
    engine = DeterministicEmbeddingEngine(dimension=4)
    custom = [0.5, 0.5, 0.5, 0.5]
    engine.register_custom_vector("special text", custom)
    assert engine.generate_vector("special text") == custom


# ==============================================================================
# 2. Mock Controllers Unit Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_llm_controller_queue_and_recording():
    """Verify LLM controller queues responses and records requests in order."""
    controller = MockLLMController()
    controller.queue_text_response("First answer")
    controller.queue_tool_call(
        name="knowledge_search",
        arguments={"query": "test query"},
        call_id="call_01",
    )

    req1 = RecordedRequest(
        endpoint="/v1/chat/completions",
        method="POST",
        headers={"authorization": "Bearer key"},
        body={"messages": [{"role": "user", "content": "What is clean arch?"}]},
    )
    resp1 = await controller.get_next_response(req1)
    assert resp1.content == "First answer"
    assert controller.call_count == 1
    assert controller.last_request.get_last_user_message() == "What is clean arch?"

    req2 = RecordedRequest(
        endpoint="/v1/chat/completions",
        method="POST",
        headers={},
        body={"messages": [{"role": "user", "content": "Search guidelines"}]},
    )
    resp2 = await controller.get_next_response(req2)
    assert resp2.finish_reason == "tool_calls"
    assert resp2.tool_calls[0].name == "knowledge_search"
    assert resp2.tool_calls[0].id == "call_01"
    assert controller.call_count == 2

    # Controller reset
    controller.reset()
    assert controller.call_count == 0


@pytest.mark.asyncio
async def test_llm_controller_rule_matcher():
    """Verify rule matchers trigger when response queue is empty."""
    controller = MockLLMController()
    controller.add_rule(
        match_fn=lambda req: "ping" in req.get_last_user_message().lower(),
        response_fn=lambda req: MockLLMResponse.text("pong!"),
    )

    req = RecordedRequest(
        endpoint="/v1/chat/completions",
        method="POST",
        headers={},
        body={"messages": [{"role": "user", "content": "Send me a Ping"}]},
    )
    resp = await controller.get_next_response(req)
    assert resp.content == "pong!"


@pytest.mark.asyncio
async def test_embedding_controller_batch_and_error():
    """Verify embedding controller handles batch requests and error queueing."""
    controller = MockEmbeddingController(dimension=128)

    req = RecordedRequest(
        endpoint="/v1/embeddings",
        method="POST",
        headers={},
        body={"input": ["Chunk 1", "Chunk 2", "Chunk 3"], "model": "test-bge"},
    )
    status, payload = await controller.process_embeddings(req)
    assert status == 200
    assert payload["object"] == "list"
    assert len(payload["data"]) == 3
    assert len(payload["data"][0]["embedding"]) == 128
    assert controller.call_count == 1

    # Queue an error
    controller.queue_error(429, "Rate limit exceeded")
    status_err, payload_err = await controller.process_embeddings(req)
    assert status_err == 429
    assert "Rate limit exceeded" in payload_err["error"]["message"]


# ==============================================================================
# 3. ASGI Application Endpoint Tests (In-Process Transport)
# ==============================================================================

@pytest.mark.asyncio
async def test_asgi_health_and_completions():
    """Verify Starlette ASGI app endpoints with httpx.ASGITransport."""
    llm = MockLLMController()
    embedding = MockEmbeddingController(dimension=64)
    app = create_mock_upstream_app(llm, embedding)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        # 1. Health check
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # 2. Non-streaming Chat completion
        llm.queue_text_response("Hello from mock LLM")
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "meta-llama/Llama-3.1-8B-Instruct",
                "messages": [{"role": "user", "content": "Hi!"}],
                "stream": False,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Hello from mock LLM"
        assert data["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_asgi_streaming_chat_completions():
    """Verify Starlette ASGI app SSE streaming chat completions."""
    llm = MockLLMController()
    embedding = MockEmbeddingController(dimension=64)
    app = create_mock_upstream_app(llm, embedding)

    llm.queue_stream_response(chunks=["Alpha ", "Beta ", "Gamma."])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "meta-llama/Llama-3.1-8B-Instruct",
                "messages": [{"role": "user", "content": "Stream Greek letters"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        events = await parse_sse_stream(resp)
        assert len(events) >= 4  # Initial role + 3 text deltas + finish delta
        full_text = "".join(
            e["choices"][0]["delta"].get("content", "")
            for e in events
            if e.get("choices") and "content" in e["choices"][0]["delta"]
        )
        assert full_text == "Alpha Beta Gamma."


@pytest.mark.asyncio
async def test_asgi_anthropic_messages_endpoint():
    """Verify Anthropic messages format emulation endpoint."""
    llm = MockLLMController()
    embedding = MockEmbeddingController(dimension=64)
    app = create_mock_upstream_app(llm, embedding)

    llm.queue_tool_call(
        name="knowledge_get",
        arguments={"id": "item-123"},
        call_id="toolu_01",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Fetch doc item-123"}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert data["stop_reason"] == "tool_use"
        tool_block = data["content"][0]
        assert tool_block["type"] == "tool_use"
        assert tool_block["name"] == "knowledge_get"
        assert tool_block["input"] == {"id": "item-123"}


@pytest.mark.asyncio
async def test_asgi_embeddings_endpoint():
    """Verify embedding endpoints (/v1/embeddings and /embed)."""
    llm = MockLLMController()
    embedding = MockEmbeddingController(dimension=128)
    app = create_mock_upstream_app(llm, embedding)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://mock"
    ) as client:
        resp = await client.post(
            "/v1/embeddings",
            json={"input": "Test embedding", "model": "BAAI/bge-large-en-v1.5"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 1
        assert len(data["data"][0]["embedding"]) == 128

        # TEI compatibility endpoint
        resp_tei = await client.post(
            "/embed",
            json={"input": ["Batch 1", "Batch 2"]},
        )
        assert resp_tei.status_code == 200
        assert len(resp_tei.json()["data"]) == 2


# ==============================================================================
# 4. Ephemeral Port Standalone Server Manager Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_standalone_ephemeral_server_lifecycle():
    """Verify spinning up Uvicorn on Port 0, querying via real socket, and clean teardown."""
    manager = MockServerManager(embedding_dimension=256)
    manager.llm.queue_text_response("Socket server response")

    async with manager:
        assert manager.port > 0
        assert manager.base_url.startswith("http://127.0.0.1:")

        async with httpx.AsyncClient(base_url=manager.base_url) as client:
            # Test health
            resp_health = await client.get("/health")
            assert resp_health.status_code == 200

            # Test chat completions
            resp_chat = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "Ping"}],
                },
            )
            assert resp_chat.status_code == 200
            assert resp_chat.json()["choices"][0]["message"]["content"] == "Socket server response"

            # Test embeddings
            resp_emb = await client.post(
                "/v1/embeddings",
                json={"input": "Socket embedding test"},
            )
            assert resp_emb.status_code == 200
            assert len(resp_emb.json()["data"][0]["embedding"]) == 256

    # Verify server stopped
    assert manager._server_task is None
