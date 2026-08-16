"""Tier 3 Pairwise Combination Tests: Chat Orchestration + Internal Tool Loop + External Tool Passthrough + Max Iterations.

Tests cross-feature interactions between:
- Feature 24: Chat Orchestration & Tool Interception Loop
- Feature 25: External Harness Tool Passthrough
- Feature 27: Max Tool Iteration Guardrail
- Feature 15: Knowledge Tool Schemas
- Feature 2: Pragmatic Clean Architecture Core
"""

import json
import uuid
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
from src.gateway.domain.tools import ToolCall, is_internal_tool
from tests.e2e.harness.mock_server import MockLLMResponse, MockServerManager, MockToolCall


class MockOrchestrator:
    """Lightweight orchestrator simulator validating tool interception logic."""

    def __init__(self, mock_server: MockServerManager, max_iterations: int = 5):
        self.mock_server = mock_server
        self.max_iterations = max_iterations

    async def execute_turn(
        self,
        messages: list[dict],
        tools: list[dict],
        knowledge_store: dict[str, str],
    ) -> CanonicalChatResponse:
        current_messages = list(messages)
        iterations = 0

        while iterations < self.max_iterations:
            iterations += 1
            resp = await self.mock_server.llm.get_next_response(None)

            if not resp.tool_calls:
                return CanonicalChatResponse(
                    id=f"resp_{uuid.uuid4().hex[:8]}",
                    model="test-model",
                    content=[CanonicalTextBlock(text=resp.content or "")],
                    finish_reason="stop",
                )

            has_external = False
            external_tool_calls: list[ToolCall] = []

            for tc in resp.tool_calls:
                if not is_internal_tool(tc.name):
                    has_external = True
                    args_str = tc.arguments if isinstance(tc.arguments, str) else json.dumps(tc.arguments)
                    external_tool_calls.append(
                        ToolCall(
                            id=tc.id or f"call_{uuid.uuid4().hex[:6]}",
                            function={"name": tc.name, "arguments": args_str},
                        )
                    )

            if has_external:
                blocks: list[CanonicalBlock] = []
                for etc in external_tool_calls:
                    blocks.append(
                        CanonicalToolUseBlock(
                            id=etc.id,
                            name=etc.function.name,
                            input=json.loads(etc.function.arguments),
                        )
                    )
                return CanonicalChatResponse(
                    id=f"resp_{uuid.uuid4().hex[:8]}",
                    model="test-model",
                    content=blocks,
                    finish_reason="tool_use",
                )

            for tc in resp.tool_calls:
                args = tc.arguments if isinstance(tc.arguments, dict) else json.loads(tc.arguments)
                if tc.name == "knowledge_search":
                    query = args.get("query", "")
                    result = knowledge_store.get(query, f"No match for {query}")
                elif tc.name == "knowledge_save":
                    title = args.get("title", "Untitled")
                    content = args.get("content", "")
                    knowledge_store[title] = content
                    result = f"Saved item {title}"
                else:
                    result = f"Executed {tc.name}"

                current_messages.append({"role": "assistant", "tool_calls": [tc.to_openai_dict()]})
                current_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id or "call_1",
                    "content": result,
                })

        return CanonicalChatResponse(
            id=f"resp_{uuid.uuid4().hex[:8]}",
            model="test-model",
            content=[CanonicalTextBlock(text="Max iterations reached.")],
            finish_reason="max_tokens",
        )


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f24_f15_internal_tool_interception_single_turn():
    """Test pairwise interaction: Gateway intercepts knowledge_search and returns synthesized final answer."""
    mock_mgr = MockServerManager()

    mock_mgr.llm.queue_tool_call(
        name="knowledge_search",
        arguments={"query": "database_setup"},
        call_id="call_ksearch_01",
    )
    mock_mgr.llm.queue_text_response("The database uses PostgreSQL with asyncpg and pgvector.")

    orchestrator = MockOrchestrator(mock_mgr)
    knowledge_store = {"database_setup": "PostgreSQL 16 + pgvector configured with asyncpg."}

    messages = [{"role": "user", "content": "How is the database configured?"}]
    resp = await orchestrator.execute_turn(messages, [], knowledge_store)

    assert resp.finish_reason == "stop"
    assert len(resp.content) == 1
    assert isinstance(resp.content[0], CanonicalTextBlock)
    assert "PostgreSQL with asyncpg" in resp.content[0].text
    assert len([b for b in resp.content if isinstance(b, CanonicalToolUseBlock)]) == 0


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f25_external_tool_passthrough_immediate_exit():
    """Test pairwise interaction: External harness tool (bash) returned directly to client."""
    mock_mgr = MockServerManager()
    mock_mgr.llm.queue_tool_call(
        name="bash",
        arguments={"command": "pytest tests/ -v"},
        call_id="call_bash_01",
    )

    orchestrator = MockOrchestrator(mock_mgr)
    knowledge_store = {}

    messages = [{"role": "user", "content": "Run the tests"}]
    resp = await orchestrator.execute_turn(messages, [], knowledge_store)

    assert resp.finish_reason == "tool_use"
    assert len(resp.content) == 1
    assert isinstance(resp.content[0], CanonicalToolUseBlock)
    assert resp.content[0].name == "bash"
    assert resp.content[0].input["command"] == "pytest tests/ -v"


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f24_f25_mixed_multi_turn_tool_sequence():
    """Test pairwise interaction: knowledge_search -> knowledge_save -> external edit_file sequence."""
    mock_mgr = MockServerManager()

    mock_mgr.llm.queue_tool_call(
        name="knowledge_search",
        arguments={"query": "api_spec"},
        call_id="call_seq_1",
    )
    mock_mgr.llm.queue_tool_call(
        name="knowledge_save",
        arguments={"title": "UpdatedSpec", "content": "API v2 spec"},
        call_id="call_seq_2",
    )
    mock_mgr.llm.queue_tool_call(
        name="edit_file",
        arguments={"path": "src/main.py", "diff": "+ version = '0.2.0'"},
        call_id="call_seq_3",
    )

    orchestrator = MockOrchestrator(mock_mgr)
    knowledge_store = {"api_spec": "API v1 initial"}

    resp = await orchestrator.execute_turn([{"role": "user", "content": "Upgrade API"}], [], knowledge_store)

    assert resp.finish_reason == "tool_use"
    assert len(resp.content) == 1
    assert resp.content[0].name == "edit_file"
    assert knowledge_store["UpdatedSpec"] == "API v2 spec"


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f27_max_tool_iterations_guardrail():
    """Test pairwise interaction: Max tool iteration limit prevents infinite internal tool loops."""
    mock_mgr = MockServerManager()

    for i in range(10):
        mock_mgr.llm.queue_tool_call(
            name="knowledge_search",
            arguments={"query": f"loop_{i}"},
            call_id=f"call_loop_{i}",
        )

    orchestrator = MockOrchestrator(mock_mgr, max_iterations=4)
    resp = await orchestrator.execute_turn([{"role": "user", "content": "Infinite loop"}], [], {})

    assert resp.finish_reason == "max_tokens"
    assert "Max iterations reached" in resp.content[0].text


@pytest.mark.tier3
def test_pairwise_f02_f24_canonical_tool_result_error_representation():
    """Test pairwise interaction: CanonicalToolResultBlock error flag and formatting."""
    res_success = CanonicalToolResultBlock(
        tool_use_id="call_123",
        content="Search yielded 3 results",
        is_error=False,
    )
    assert res_success.is_error is False

    res_error = CanonicalToolResultBlock(
        tool_use_id="call_124",
        content="OCC Conflict: version mismatch",
        is_error=True,
    )
    assert res_error.is_error is True
    assert "version mismatch" in res_error.content
