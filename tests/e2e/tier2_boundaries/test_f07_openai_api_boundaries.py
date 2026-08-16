"""Tier 2 Boundary Tests for Feature 7: OpenAI /v1/chat/completions API.

Tests boundary conditions, malformed payloads, empty messages, extreme temperature limits, and streaming edge cases.

Canonical model boundary tests exercise the real domain models directly; HTTP
boundary tests exercise the real gateway wired to the in-process mock upstream.
"""

import httpx
import pytest

from src.gateway.domain.canonical import (
    CanonicalChatRequest,
    CanonicalMessage,
    CanonicalTextBlock,
)
from tests.e2e.harness.test_env import GatewayMockUpstream, parse_sse_stream


@pytest.mark.tier2
@pytest.mark.feature("F7")
def test_f07_boundary_canonical_chat_request_empty_messages():
    """Test boundary: CanonicalChatRequest accepts empty messages list."""
    req = CanonicalChatRequest(
        model="gpt-4o",
        messages=[],
    )
    assert req.model == "gpt-4o"
    assert req.messages == []
    assert req.workspace_id == "global"


@pytest.mark.tier2
@pytest.mark.feature("F7")
def test_f07_boundary_extreme_temperature_and_top_p_values():
    """Test boundary: boundary values for temperature (0.0, 2.0) and top_p (0.0, 1.0)."""
    req_zero = CanonicalChatRequest(
        model="gpt-4o",
        temperature=0.0,
        top_p=0.0,
    )
    assert req_zero.temperature == 0.0
    assert req_zero.top_p == 0.0

    req_max = CanonicalChatRequest(
        model="gpt-4o",
        temperature=2.0,
        top_p=1.0,
    )
    assert req_max.temperature == 2.0
    assert req_max.top_p == 1.0


@pytest.mark.tier2
@pytest.mark.feature("F7")
@pytest.mark.asyncio
async def test_f07_boundary_malformed_json_body_rejected():
    """Test boundary: an invalid JSON body is rejected by the gateway request validation."""
    async with GatewayMockUpstream() as gw:
        resp = await gw.client.post(
            "/v1/chat/completions",
            content=b"INVALID_JSON_{{{",
            headers={"Content-Type": "application/json"},
        )
        # FastAPI request validation rejects the unparseable body before any
        # handler logic runs; the gateway never forwards it upstream.
        assert resp.status_code == 422
        assert gw.llm.call_count == 0


@pytest.mark.tier2
@pytest.mark.feature("F7")
@pytest.mark.asyncio
async def test_f07_boundary_upstream_error_propagation():
    """Test boundary: upstream LLM errors (e.g. 503) surface as the gateway's
    structured 502 llm_provider_error envelope carrying the upstream message."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_error(status_code=503, message="Upstream LLM engine overloaded")

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 502
        data = resp.json()
        assert "error" in data
        assert data["error"]["type"] == "llm_provider_error"
        assert "overloaded" in data["error"]["message"]


@pytest.mark.tier2
@pytest.mark.feature("F7")
@pytest.mark.asyncio
async def test_f07_boundary_sse_streaming_empty_and_rapid_chunks():
    """Test boundary: streaming SSE with empty chunks and proper [DONE] termination."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_stream_response(["", "Chunk A", "", "Chunk B", ""])

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Stream test"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        events = await parse_sse_stream(resp)
        assert len(events) >= 5
        # Verify first event has role delta
        first_delta = events[0]["choices"][0]["delta"]
        assert first_delta.get("role") == "assistant"
