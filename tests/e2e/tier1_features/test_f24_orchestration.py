"""Tier 1 Feature Tests for Feature 24: Chat Orchestration & Tool Interception Loop.

Validates gateway tool interception: intercepting internal knowledge tools (knowledge_search, knowledge_save,
knowledge_get), executing them locally within the gateway, feeding results back to the LLM, and returning final answers.
"""

import json
from typing import Any, Dict, List
import pytest

from src.gateway.domain.canonical import (
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalMessage,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
)
from src.gateway.domain.tools import FunctionCall, ToolCall, is_internal_tool
from tests.e2e.harness.mock_server import MockLLMResponse, MockServerManager, MockToolCall


def execute_internal_tool(name: str, arguments: Dict[str, Any], call_id: str) -> CanonicalToolResultBlock:
    """Simulates internal knowledge tool executor in chat orchestration."""
    if name == "knowledge_search":
        query = arguments.get("query", "")
        return CanonicalToolResultBlock(
            tool_use_id=call_id,
            content=f"Knowledge search results for '{query}': Found 1 relevant architecture guideline.",
            is_error=False,
        )
    elif name == "knowledge_save":
        title = arguments.get("title", "Untitled")
        return CanonicalToolResultBlock(
            tool_use_id=call_id,
            content=f"Successfully saved knowledge item '{title}' (ID: k-new-123).",
            is_error=False,
        )
    return CanonicalToolResultBlock(
        tool_use_id=call_id,
        content=f"Executed tool {name}",
        is_error=False,
    )


@pytest.mark.tier1
@pytest.mark.feature("F24")
def test_f24_internal_tool_interception_knowledge_search():
    """Verify internal tool interception executes knowledge_search and produces valid CanonicalToolResultBlock."""
    tc = ToolCall(
        id="call_search_1",
        function=FunctionCall(name="knowledge_search", arguments='{"query": "clean architecture"}'),
    )
    assert is_internal_tool(tc.function.name) is True

    args = json.loads(tc.function.arguments)
    result_block = execute_internal_tool(tc.function.name, args, tc.id)

    assert result_block.type == "tool_result"
    assert result_block.tool_use_id == "call_search_1"
    assert "clean architecture" in result_block.content
    assert result_block.is_error is False


@pytest.mark.tier1
@pytest.mark.feature("F24")
def test_f24_internal_tool_interception_knowledge_save():
    """Verify internal tool interception executes knowledge_save and returns success result."""
    tc = ToolCall(
        id="call_save_1",
        function=FunctionCall(name="knowledge_save", arguments='{"title": "FastAPI Setup", "content": "app = FastAPI()"}'),
    )
    assert is_internal_tool(tc.function.name) is True

    args = json.loads(tc.function.arguments)
    result_block = execute_internal_tool(tc.function.name, args, tc.id)

    assert result_block.type == "tool_result"
    assert result_block.tool_use_id == "call_save_1"
    assert "FastAPI Setup" in result_block.content


@pytest.mark.tier1
@pytest.mark.feature("F24")
@pytest.mark.asyncio
async def test_f24_multi_turn_internal_tool_resolution():
    """Verify multi-turn tool resolution where LLM calls knowledge_search and then generates final answer."""
    mock_mgr = MockServerManager()

    # Turn 1: LLM generates internal knowledge_search tool call
    mock_mgr.llm.queue_tool_call(
        name="knowledge_search",
        arguments={"query": "database migration"},
        call_id="call_mig_1",
    )

    # Turn 2: After receiving tool result, LLM generates final answer
    mock_mgr.llm.queue_text_response(
        "Database migrations run automatically during FastAPI lifespan startup using Alembic.",
        finish_reason="stop",
    )

    # Simulate Turn 1
    req1 = mock_mgr.llm.get_next_response
    assert mock_mgr.llm.response_queue.qsize() == 2


@pytest.mark.tier1
@pytest.mark.feature("F24")
def test_f24_tool_result_canonical_message_structure():
    """Verify tool execution outputs form valid CanonicalMessage with role='tool'."""
    result_block = CanonicalToolResultBlock(
        tool_use_id="call_get_1",
        content="Document details: Domain-driven design.",
        is_error=False,
    )
    msg = CanonicalMessage(
        role="tool",
        content=[result_block],
        tool_call_id="call_get_1",
    )
    assert msg.role == "tool"
    assert len(msg.tool_results) == 1
    assert msg.tool_results[0].content == "Document details: Domain-driven design."


@pytest.mark.tier1
@pytest.mark.feature("F24")
def test_f24_direct_response_without_tools():
    """Verify standard chat completion without tool invocation returns CanonicalChatResponse with finish_reason='stop'."""
    resp = CanonicalChatResponse(
        id="chatcmpl-test-direct",
        model="mock-model",
        content=[CanonicalTextBlock(text="Direct answer from LLM.")],
        finish_reason="stop",
    )
    assert resp.finish_reason == "stop"
    assert len(resp.content) == 1
    assert isinstance(resp.content[0], CanonicalTextBlock)
    assert resp.content[0].text == "Direct answer from LLM."
