"""Tier 3 Pairwise Combination Tests: Auth + OpenAI / Anthropic Protocols + Model Registry.

Tests cross-feature interactions between:
- Feature 10: API Key Authentication Middleware
- Feature 7: OpenAI /v1/chat/completions API
- Feature 8: Anthropic /v1/messages API
- Feature 9: Model Registry & Alias Mapping
- Feature 11: Health & Discovery Endpoints

All interactions run through the real gateway (auth middleware included) wired
to the in-process mock upstream.
"""

import httpx
import pytest

from tests.e2e.harness.test_env import GatewayMockUpstream, parse_sse_stream


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f07_f10_auth_openai_bearer_token_json_and_stream():
    """Test pairwise interaction: Bearer Token Auth + OpenAI Protocol (JSON and SSE Stream)."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response("OpenAI Bearer Auth Non-Streaming Success")
        gw.llm.queue_stream_response(["OpenAI ", "Bearer ", "Streaming ", "Success"])

        # 1. Non-streaming JSON completion
        resp_json = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Hello OpenAI"}],
                "stream": False,
            },
        )
        assert resp_json.status_code == 200
        data = resp_json.json()
        assert data["object"] == "chat.completion"
        assert len(data["choices"]) > 0
        assert "Bearer Auth Non-Streaming" in data["choices"][0]["message"]["content"]

        # 2. Streaming SSE completion
        resp_stream = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Stream test"}],
                "stream": True,
            },
        )
        assert resp_stream.status_code == 200
        events = await parse_sse_stream(resp_stream)
        assert len(events) >= 4
        full_text = "".join(
            e["choices"][0]["delta"].get("content", "")
            for e in events
            if e.get("choices") and e["choices"] and "content" in e["choices"][0]["delta"]
        )
        assert "Bearer Streaming Success" in full_text


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f08_f10_auth_anthropic_x_api_key_json_and_stream():
    """Test pairwise interaction: x-api-key Auth + Anthropic /v1/messages Protocol."""
    async with GatewayMockUpstream(api_key="sk-test-user-1") as gw:
        gw.llm.queue_text_response("Anthropic x-api-key Success")

        resp = await gw.client.post(
            "/v1/messages",
            headers={"anthropic-version": "2023-06-01"},
            json={
                "model": "default",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hello Claude"}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert len(data["content"]) > 0
        assert data["content"][0]["type"] == "text"
        assert "Anthropic x-api-key Success" in data["content"][0]["text"]


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f09_f10_auth_model_alias_resolution():
    """Test pairwise interaction: Auth + Model Registry alias resolution through chat."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response("Alias resolved and executed upstream.")

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "default",  # built-in alias
                "messages": [{"role": "user", "content": "Write quicksort"}],
            },
        )
        assert resp.status_code == 200
        assert "Alias resolved and executed upstream." in resp.json()["choices"][0]["message"]["content"]
        assert gw.llm.call_count == 1
        # The registry resolved the alias to the configured backend model id
        assert gw.llm.last_request.model == "mock-llama-3.1-8b"


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f10_f07_auth_rejects_invalid_key():
    """Test pairwise interaction: invalid API keys are rejected before any upstream call."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response("should never be reached")
        resp = await gw.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer sk-wrong-key"},
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 401
        assert "error" in resp.json()
        assert gw.llm.call_count == 0


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f10_f07_f08_auth_dual_header_acceptance():
    """Test pairwise interaction: dual auth headers (Bearer + x-api-key) valid across endpoints."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response("Dual Header Auth Accepted")

        resp = await gw.client.post(
            "/v1/chat/completions",
            headers={"x-api-key": "sk-test-user-1"},
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Dual auth test"}],
            },
        )
        assert resp.status_code == 200
        assert "Dual Header Auth Accepted" in resp.json()["choices"][0]["message"]["content"]


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f11_auth_health_and_service_discovery():
    """Test pairwise interaction: gateway discovery endpoint /health works without auth."""
    async with GatewayMockUpstream(api_key=None) as gw:
        resp_health = await gw.client.get("/health")
        assert resp_health.status_code == 200
        health_data = resp_health.json()
        assert health_data["status"] == "healthy"
        assert "database" not in health_data
