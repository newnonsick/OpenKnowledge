"""Tier 4 Real-World Scenario: Automated Internal Tool Interception Workflow.

Simulates the gateway executing a 3-turn automated loop:
Turn 1: LLM calls `knowledge_search` (intercepted)
Turn 2: LLM calls `knowledge_save` (intercepted)
Turn 3: LLM generates final synthesized answer
Verifies that no internal tool calls are exposed to the external client.
"""

import json
import uuid
import httpx
import pytest

from src.gateway.domain.canonical import (
    CanonicalChatResponse,
    CanonicalTextBlock,
    CanonicalToolUseBlock,
)
from src.gateway.domain.tools import is_internal_tool
from tests.e2e.harness.mock_server import MockLLMResponse, MockServerManager


class GatewayInterceptionLoop:
    """Simulates the Gateway Chat Orchestrator executing the tool interception loop."""

    def __init__(self, mock_mgr: MockServerManager, max_iterations: int = 5):
        self.mock_mgr = mock_mgr
        self.max_iterations = max_iterations

    async def run_chat_session(
        self,
        user_prompt: str,
        knowledge_db: dict[str, str],
    ) -> CanonicalChatResponse:
        messages = [{"role": "user", "content": user_prompt}]
        iteration = 0

        while iteration < self.max_iterations:
            iteration += 1
            # Fetch next LLM decision
            resp = await self.mock_mgr.llm.get_next_response(None)

            # If LLM produces text completion with no tools -> Finished
            if not resp.tool_calls:
                return CanonicalChatResponse(
                    id=f"chat_{uuid.uuid4().hex[:8]}",
                    model="test-model",
                    content=[CanonicalTextBlock(text=resp.content or "")],
                    finish_reason="stop",
                )

            # Check if any external tool is requested
            for tc in resp.tool_calls:
                if not is_internal_tool(tc.name):
                    # External tool: return immediately to client
                    args = tc.arguments if isinstance(tc.arguments, dict) else json.loads(tc.arguments)
                    return CanonicalChatResponse(
                        id=f"chat_{uuid.uuid4().hex[:8]}",
                        model="test-model",
                        content=[
                            CanonicalToolUseBlock(
                                id=tc.id or "call_ext",
                                name=tc.name,
                                input=args,
                            )
                        ],
                        finish_reason="tool_use",
                    )

            # All tool calls are internal: execute locally and loop back to LLM
            for tc in resp.tool_calls:
                args = tc.arguments if isinstance(tc.arguments, dict) else json.loads(tc.arguments)
                if tc.name == "knowledge_search":
                    query = args.get("query", "")
                    tool_output = knowledge_db.get(query, f"No knowledge found for: {query}")
                elif tc.name == "knowledge_save":
                    title = args.get("title", "Untitled")
                    content = args.get("content", "")
                    knowledge_db[title] = content
                    tool_output = f"Successfully saved knowledge item: {title}"
                elif tc.name == "knowledge_update":
                    item_id = args.get("item_id", "")
                    content = args.get("content", "")
                    knowledge_db[item_id] = content
                    tool_output = f"Successfully updated knowledge item: {item_id}"
                else:
                    tool_output = f"Executed {tc.name}"

                messages.append({"role": "assistant", "tool_calls": [tc.to_openai_dict()]})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id or "call_id",
                    "content": tool_output,
                })

        return CanonicalChatResponse(
            id=f"chat_{uuid.uuid4().hex[:8]}",
            model="test-model",
            content=[CanonicalTextBlock(text="Max iterations exceeded.")],
            finish_reason="max_tokens",
        )


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_tool_interception_three_turn_workflow():
    """Scenario: 3-turn loop with knowledge_search -> knowledge_save -> final answer returned to client."""
    mock_mgr = MockServerManager()

    # Turn 1: LLM asks for knowledge_search
    mock_mgr.llm.queue_tool_call(
        name="knowledge_search",
        arguments={"query": "SSE guidelines"},
        call_id="call_ksearch_01",
    )
    # Turn 2: LLM saves new standard via knowledge_save
    mock_mgr.llm.queue_tool_call(
        name="knowledge_save",
        arguments={
            "title": "SSE Keepalive Standard",
            "content": "SSE connections must emit keepalive comment ': keepalive' every 15 seconds.",
        },
        call_id="call_ksave_02",
    )
    # Turn 3: LLM provides final user-facing summary
    final_text = (
        "I checked our existing SSE guidelines and persisted the new 'SSE Keepalive Standard' "
        "specifying 15-second heartbeat intervals."
    )
    mock_mgr.llm.queue_text_response(final_text)

    knowledge_db = {"SSE guidelines": "Current SSE standard specifies UTF-8 encoding."}
    loop = GatewayInterceptionLoop(mock_mgr)

    response = await loop.run_chat_session(
        user_prompt="Review SSE guidelines and add a 15-second heartbeat requirement.",
        knowledge_db=knowledge_db,
    )

    # External client must receive ONLY final text response
    assert response.finish_reason == "stop"
    assert len(response.content) == 1
    assert isinstance(response.content[0], CanonicalTextBlock)
    assert "SSE Keepalive Standard" in response.content[0].text
    # Verify knowledge_db updated internally
    assert "SSE Keepalive Standard" in knowledge_db
    assert "15 seconds" in knowledge_db["SSE Keepalive Standard"]
    # Verify mock LLM was called 3 times
    assert mock_mgr.llm.call_count == 3


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_tool_interception_with_knowledge_update_occ():
    """Scenario: Multi-turn loop executing OCC update inside the gateway without client exposure."""
    mock_mgr = MockServerManager()

    mock_mgr.llm.queue_tool_call(
        name="knowledge_update",
        arguments={
            "item_id": "guideline_001",
            "expected_version": 1,
            "content": "Updated guideline v2",
        },
        call_id="call_kupdate_01",
    )
    mock_mgr.llm.queue_text_response("Successfully updated guideline_001 to version 2.")

    knowledge_db = {"guideline_001": "Guideline v1"}
    loop = GatewayInterceptionLoop(mock_mgr)

    response = await loop.run_chat_session(
        user_prompt="Update guideline_001 content to v2.",
        knowledge_db=knowledge_db,
    )

    assert response.finish_reason == "stop"
    assert "Successfully updated guideline_001" in response.content[0].text
    assert knowledge_db["guideline_001"] == "Updated guideline v2"
    assert mock_mgr.llm.call_count == 2
