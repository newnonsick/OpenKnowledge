"""Tier 2 Boundary Tests for Feature 8: Anthropic /v1/messages API.

Tests boundary conditions, Anthropic format schemas, alternating role requirements, and tool use content blocks.
"""

import httpx
import pytest

from src.gateway.domain.canonical import (
    CanonicalChatRequest,
    CanonicalMessage,
    CanonicalTextBlock,
    CanonicalToolUseBlock,
)
from tests.e2e.harness.mock_server import MockLLMResponse, MockServerManager, MockToolCall


@pytest.mark.tier2
@pytest.mark.feature("F8")
@pytest.mark.asyncio
async def test_f08_boundary_anthropic_malformed_json_body():
    """Test boundary: invalid JSON body is rejected by gateway request validation."""
    from src.gateway.config import settings
    from src.gateway.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://gateway-test",
        headers={"x-api-key": settings.gateway.gateway_api_keys[0]},
    ) as client:
        resp = await client.post(
            "/v1/messages",
            content=b"INVALID_JSON_{{{",
            headers={"Content-Type": "application/json"},
        )
        # FastAPI request validation rejects the unparseable body
        assert resp.status_code == 422


@pytest.mark.tier2
@pytest.mark.feature("F8")
@pytest.mark.asyncio
async def test_f08_boundary_anthropic_missing_messages_or_empty_list():
    """Test boundary: the gateway rejects an empty messages list with a 400
    invalid_request_error instead of forwarding it to the upstream backend."""
    from src.gateway.config import settings
    from src.gateway.main import create_app
    from src.gateway.presentation.routers.messages import get_llm_client

    class _FailIfCalled:
        async def generate(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("upstream must not be called for empty messages")

        async def generate_stream(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("upstream must not be called for empty messages")
            yield

    app = create_app()
    app.dependency_overrides[get_llm_client] = _FailIfCalled
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://gateway-test",
        headers={"x-api-key": settings.gateway.gateway_api_keys[0]},
    ) as client:
        resp = await client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [],
            },
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["type"] == "error"
        assert data["error"]["type"] == "invalid_request_error"


@pytest.mark.tier2
@pytest.mark.feature("F8")
@pytest.mark.asyncio
async def test_f08_boundary_anthropic_tool_use_content_blocks():
    """Test boundary: Anthropic response with tool_use content blocks and stop_reason='tool_use'
    for an external tool passed through the gateway."""
    from tests.e2e.harness.test_env import GatewayMockUpstream

    async with GatewayMockUpstream() as gw:
        gw.llm.queue_tool_call(
            name="web_search",
            arguments={"query": "boundary query"},
            call_id="toolu_test_123",
        )

        resp = await gw.client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Find info"}],
                "tools": [
                    {
                        "name": "web_search",
                        "description": "Search the web",
                        "input_schema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    }
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["stop_reason"] == "tool_use"
        tool_blocks = [b for b in data["content"] if b.get("type") == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0]["name"] == "web_search"
        assert tool_blocks[0]["input"] == {"query": "boundary query"}


@pytest.mark.tier2
@pytest.mark.feature("F8")
def test_f08_boundary_canonical_representation_of_anthropic_blocks():
    """Test boundary: CanonicalToolUseBlock properly serializes nested dictionary arguments."""
    tb = CanonicalToolUseBlock(
        id="toolu_abc_456",
        name="custom_tool",
        input={"param1": "val1", "nested": {"count": 42}},
    )
    assert tb.type == "tool_use"
    assert tb.id == "toolu_abc_456"
    assert tb.input["nested"]["count"] == 42


@pytest.mark.tier2
@pytest.mark.feature("F8")
@pytest.mark.asyncio
async def test_f08_boundary_anthropic_error_forwarding():
    """Test boundary: upstream backend 502/500 errors surface as the gateway's
    Anthropic 502 error envelope carrying the upstream message."""
    from tests.e2e.harness.test_env import GatewayMockUpstream

    async with GatewayMockUpstream() as gw:
        gw.llm.queue_error(status_code=502, message="Anthropic backend upstream timeout")

        resp = await gw.client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 502
        data = resp.json()
        assert data["type"] == "error"
        assert data["error"]["type"] == "llm_provider_error"
        assert "timeout" in data["error"]["message"]
