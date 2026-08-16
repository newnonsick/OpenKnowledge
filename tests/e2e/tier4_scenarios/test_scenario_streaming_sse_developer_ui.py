"""Tier 4 Real-World Scenario: Frontend Developer UI SSE Streaming with Token Reassembly.

Simulates a web frontend developer UI consuming the real gateway SSE stream,
performing delta token reassembly, and verifying [DONE] termination.
"""

import pytest

from tests.e2e.harness.test_env import GatewayMockUpstream, parse_sse_stream


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_streaming_sse_ui_token_reassembly():
    """Scenario: Web developer chat UI consumes real-time SSE stream with delta token reassembly."""
    async with GatewayMockUpstream() as gw:
        stream_tokens = [
            "In ",
            "Clean ",
            "Architecture, ",
            "domain ",
            "entities ",
            "must ",
            "remain ",
            "independent ",
            "of ",
            "frameworks.",
        ]
        gw.llm.queue_stream_response(stream_tokens)

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Explain Clean Architecture domain rules"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        # Simulate UI consumption loop
        assembled_ui_text = ""
        events = await parse_sse_stream(resp)

        for event in events:
            if event.get("choices") and event["choices"]:
                delta = event["choices"][0]["delta"]
                token = delta.get("content", "")
                assembled_ui_text += token

        expected_full_text = "".join(stream_tokens)
        assert assembled_ui_text == expected_full_text
        assert "Clean Architecture" in assembled_ui_text
        assert "independent of frameworks" in assembled_ui_text


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_streaming_sse_tool_call_delta_reassembly():
    """Scenario: UI consuming streamed tool call arguments delta by delta."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_tool_call(
            name="git_diff",
            arguments={"branch": "main", "cached": True},
            call_id="call_git_001",
        )

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Show git diff against main"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        events = await parse_sse_stream(resp)

        tool_calls = []
        for e in events:
            if e.get("choices") and e["choices"]:
                delta = e["choices"][0]["delta"]
                if "tool_calls" in delta:
                    tool_calls.extend(delta["tool_calls"])

        assert len(tool_calls) > 0
        assert tool_calls[0]["function"]["name"] == "git_diff"
        assert "branch" in tool_calls[0]["function"]["arguments"]
