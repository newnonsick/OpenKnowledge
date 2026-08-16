"""Tier 4 Real-World Scenario: Anthropic Claude Messages External Tool Passthrough.

Simulates an external coding agent harness (e.g. Cline, Roo Code, Aider)
using the real gateway's Anthropic /v1/messages API with tool calling
(e.g. `bash`, `read_file`), including the full tool_result round-trip.
"""

import pytest

from tests.e2e.harness.test_env import GatewayMockUpstream


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_anthropic_external_tool_passthrough_loop():
    """Scenario: Anthropic client invokes external tools -> Gateway passes through tool_use -> Client returns tool_result."""
    from tests.e2e.harness.mock_server import MockLLMResponse

    async with GatewayMockUpstream(api_key="sk-test-user-1") as gw:
        # Step 1: LLM decides to invoke bash command
        gw.llm.queue_response(
            MockLLMResponse.tool_call(
                name="bash",
                arguments={"command": "pytest tests/e2e/tier1_features/ -v"},
                call_id="toolu_bash_01",
            )
        )

        # Step 2: LLM receives command execution result and concludes
        gw.llm.queue_text_response(
            "All 140 Tier 1 tests passed successfully with 100% pass rate."
        )

        headers = {"anthropic-version": "2023-06-01"}

        # 1. First turn: user asks to run test suite
        messages = [{"role": "user", "content": "Run our Tier 1 test suite."}]
        tools = [
            {
                "name": "bash",
                "description": "Execute a shell command",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }
        ]

        resp1 = await gw.client.post(
            "/v1/messages",
            headers=headers,
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": messages,
                "tools": tools,
            },
        )
        assert resp1.status_code == 200
        data1 = resp1.json()
        assert data1["type"] == "message"
        assert data1["stop_reason"] == "tool_use"

        # Verify tool_use block
        tool_blocks = [b for b in data1["content"] if b["type"] == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0]["name"] == "bash"
        assert tool_blocks[0]["input"]["command"] == "pytest tests/e2e/tier1_features/ -v"

        # 2. Second turn: client feeds back tool_result
        messages.append({"role": "assistant", "content": data1["content"]})
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_blocks[0]["id"],
                    "content": "================ 140 passed in 4.2s ================",
                    "is_error": False,
                }
            ],
        })

        resp2 = await gw.client.post(
            "/v1/messages",
            headers=headers,
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": messages,
                "tools": tools,
            },
        )
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["stop_reason"] == "end_turn"
        assert len(data2["content"]) > 0
        assert "140 Tier 1 tests passed" in data2["content"][0]["text"]

        # The tool result completed the full round-trip upstream
        assert gw.llm.call_count == 2
        tool_msgs = [m for m in gw.llm.recorded_requests[1].messages if m.get("role") == "tool"]
        assert tool_msgs, "tool_result must be forwarded upstream as a tool message"
        assert "140 passed in 4.2s" in tool_msgs[0]["content"]


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_anthropic_multi_tool_call_in_single_turn():
    """Scenario: Anthropic model invoking multiple external tools in a single response."""
    from tests.e2e.harness.mock_server import MockLLMResponse, MockToolCall

    async with GatewayMockUpstream(api_key="sk-test-user-1") as gw:
        tc1 = MockToolCall(name="read_file", arguments={"path": "src/main.py"}, id="toolu_read_1")
        tc2 = MockToolCall(name="read_file", arguments={"path": "src/config.py"}, id="toolu_read_2")

        gw.llm.queue_response(
            MockLLMResponse.multiple_tool_calls(
                tools=[tc1, tc2],
                content="I will inspect both files.",
            )
        )

        resp = await gw.client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Check main.py and config.py"}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["stop_reason"] == "tool_use"
        tool_blocks = [b for b in data["content"] if b["type"] == "tool_use"]
        assert len(tool_blocks) == 2
        assert tool_blocks[0]["input"]["path"] == "src/main.py"
        assert tool_blocks[1]["input"]["path"] == "src/config.py"
        # Preamble text emitted alongside the tool calls survives conversion
        text_blocks = [b for b in data["content"] if b["type"] == "text"]
        assert text_blocks and "inspect both files" in text_blocks[0]["text"]
