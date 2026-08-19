"""Tier 1 Feature Tests for Feature 8: Anthropic /v1/messages API.

Validates standard Anthropic messages non-streaming JSON responses, tool_use content blocks,
system prompt handling, usage accounting, and error structures.

All HTTP tests exercise the real gateway stack (auth middleware, Anthropic converter,
orchestrator, HTTP LLM adapter) wired to the in-process mock upstream.
"""

import httpx
import pytest

from tests.e2e.harness.test_env import GatewayMockUpstream


@pytest.mark.tier1
@pytest.mark.feature("F8")
@pytest.mark.asyncio
async def test_f08_anthropic_messages_non_streaming_json():
    """Verify Anthropic /v1/messages standard non-streaming response structure."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response(
            "Pragmatic Clean Architecture isolates domain logic from infrastructure.",
            finish_reason="stop",
        )

        resp = await gw.client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Explain architecture design."}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert len(data["content"]) >= 1
        assert data["content"][0]["type"] == "text"
        assert "Pragmatic Clean Architecture" in data["content"][0]["text"]
        assert data["stop_reason"] == "end_turn"
        assert "usage" in data
        assert data["usage"]["input_tokens"] > 0


@pytest.mark.tier1
@pytest.mark.feature("F8")
@pytest.mark.asyncio
async def test_f08_anthropic_messages_tool_use_content_block():
    """Verify Anthropic tool_use content block formatting and stop_reason for external tools."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_tool_call(
            name="web_search",
            arguments={"query": "database migration guide"},
            call_id="toolu_search_01",
        )

        resp = await gw.client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "messages": [{"role": "user", "content": "Search for migration guide"}],
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
        assert tool_blocks[0]["input"]["query"] == "database migration guide"


@pytest.mark.tier1
@pytest.mark.feature("F8")
@pytest.mark.asyncio
async def test_f08_anthropic_system_prompt_handling():
    """Verify Anthropic top-level system parameter is forwarded upstream as a system message."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response("Understood. I will respond in bullet points.")

        resp = await gw.client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "system": "You are a concise engineering assistant. Always respond in bullet points.",
                "messages": [{"role": "user", "content": "List 3 advantages of microservices."}],
            },
        )
        assert resp.status_code == 200
        assert gw.llm.last_request is not None
        upstream_messages = gw.llm.last_request.messages
        assert upstream_messages, "upstream must receive messages"
        assert upstream_messages[0]["role"] == "system"
        upstream_system = upstream_messages[0]["content"]
        assert upstream_system.startswith(
            "You are a concise engineering assistant. Always respond in bullet points."
        )
        assert upstream_system.count("# Long-Term Knowledge and Memory") == 1
        assert "Only save or update knowledge when the user explicitly asks you to do so." in upstream_system


@pytest.mark.tier1
@pytest.mark.feature("F8")
@pytest.mark.asyncio
async def test_f08_anthropic_token_usage_accounting():
    """Verify input_tokens and output_tokens usage fields in Anthropic response."""
    from tests.e2e.harness.mock_server import MockLLMResponse

    async with GatewayMockUpstream() as gw:
        custom_resp = MockLLMResponse.text(
            content="Response text.",
            prompt_tokens=42,
            completion_tokens=18,
        )
        gw.llm.queue_response(custom_resp)

        resp = await gw.client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "messages": [{"role": "user", "content": "Check token usage."}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["usage"]["input_tokens"] == 42
        assert data["usage"]["output_tokens"] == 18


@pytest.mark.tier1
@pytest.mark.feature("F8")
@pytest.mark.asyncio
async def test_f08_anthropic_error_envelope_format():
    """Verify the Anthropic error envelope when the upstream backend fails.

    Upstream failures surface as a 502 llm_provider_error envelope in the
    Anthropic {"type": "error", "error": {...}} shape without provider detail.
    """
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_error(
            status_code=400,
            message="Invalid parameter: max_tokens must be positive",
        )

        resp = await gw.client.post(
            "/v1/messages",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert resp.status_code == 502
        data = resp.json()
        assert data["type"] == "error"
        assert data["error"]["type"] == "llm_provider_error"
        assert data["error"]["message"] == "A gateway dependency failed."
        assert "max_tokens must be positive" not in resp.text


@pytest.mark.tier1
@pytest.mark.feature("F8")
@pytest.mark.asyncio
async def test_f08_anthropic_empty_messages_rejected():
    """Verify the gateway rejects an empty messages array with a 400 invalid_request_error."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response("should never be used")

        resp = await gw.client.post(
            "/v1/messages",
            json={"model": "claude-3-5-sonnet", "messages": []},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert data["type"] == "error"
        assert data["error"]["type"] == "invalid_request_error"
        assert gw.llm.call_count == 0
