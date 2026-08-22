"""Unit tests for Tool Interception Engine, Internal Tool Execution, and Guardrails."""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import pytest

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.application.services.chat_orchestrator import (
    ChatOrchestratorService,
    IChatOrchestrator,
)
from src.gateway.application.services.knowledge_service import KnowledgeService
from src.gateway.application.services.retrieval_service import IRetrievalService
from src.gateway.domain.canonical import (
    BlendedSearchResult,
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalLLMResponse,
    CanonicalMessage,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
)
from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision
from src.gateway.domain.exceptions import (
    ConcurrencyConflictException,
    ItemNotFoundException,
    ToolExecutionException,
    ValidationException,
)
from src.gateway.domain.tools import (
    FunctionCall,
    FunctionDefinition,
    INTERNAL_TOOL_NAMES,
    ToolCall,
    ToolDefinition,
    ToolResult,
    get_internal_tool_definitions,
    is_internal_tool,
)


class MockLLMClient(ILLMClient):
    """Configurable mock LLM client for unit tests."""

    def __init__(self, responses: Optional[List[CanonicalLLMResponse]] = None) -> None:
        self.responses: List[CanonicalLLMResponse] = list(responses or [])
        self.call_count: int = 0
        self.call_history: List[Dict[str, Any]] = []

    def queue_response(self, response: CanonicalLLMResponse) -> None:
        self.responses.append(response)

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> CanonicalLLMResponse:
        self.call_count += 1
        self.call_history.append({
            "messages": messages,
            "tools": tools,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "kwargs": kwargs,
        })
        if self.responses:
            return self.responses.pop(0)
        return CanonicalLLMResponse(
            id="chatcmpl-default",
            model=model or "test-model",
            content="Default mock response.",
            finish_reason="stop",
        )

    async def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        self.call_count += 1
        yield  # type: ignore


@pytest.mark.unit
def test_internal_tool_detection_and_names():
    """Verify internal tool registry recognizes knowledge_* tools and excludes others."""
    assert is_internal_tool("knowledge_search") is True
    assert is_internal_tool("knowledge_get") is True
    assert is_internal_tool("knowledge_save") is True
    assert is_internal_tool("knowledge_update") is True
    assert is_internal_tool("knowledge_delete") is True

    assert is_internal_tool("bash") is False
    assert is_internal_tool("edit_file") is False
    assert is_internal_tool("git") is False
    assert is_internal_tool("mcp__query") is False
    assert is_internal_tool("") is False


@pytest.mark.unit
def test_internal_tool_definitions_schema_structure():
    """Verify internal tool definitions have valid schema properties."""
    tool_defs = get_internal_tool_definitions()
    assert len(tool_defs) == 5
    names = {td.function.name for td in tool_defs}
    assert names == INTERNAL_TOOL_NAMES
    for td in tool_defs:
        assert td.type == "function"
        assert len(td.function.description) > 0
        assert "properties" in td.function.parameters


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_definition_ingestion_merges_external_and_internal():
    """Verify tool definition ingestion combines external client tools and internal knowledge tools without duplicates."""
    llm_client = MockLLMClient()
    orchestrator = ChatOrchestratorService(llm_client=llm_client)

    external_tool = ToolDefinition(
        type="function",
        function=FunctionDefinition(
            name="bash",
            description="Execute bash command",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        ),
    )

    combined_defs, upstream_tools = orchestrator._prepare_tools([external_tool])
    assert len(combined_defs) == 2
    names = [td.function.name for td in combined_defs]
    assert names == ["bash", "knowledge_search"]

    assert upstream_tools is not None
    assert len(upstream_tools) == 2
    assert any(t["function"]["name"] == "bash" for t in upstream_tools)
    assert any(t["function"]["name"] == "knowledge_search" for t in upstream_tools)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_single_turn_internal_tool_interception():
    """Verify single turn internal tool execution: LLM calls knowledge_search -> local execution -> LLM returns answer."""
    mock_ks = MagicMock(spec=KnowledgeService)
    mock_ks.execute_tool = AsyncMock(return_value=ToolResult(
        tool_call_id="call_ksearch_01",
        name="knowledge_search",
        content=json.dumps({"results": [{"title": "Auth Guide", "content": "Bearer tokens"}], "count": 1}),
        is_error=False,
    ))

    # Turn 1: LLM calls knowledge_search
    resp1 = CanonicalLLMResponse(
        id="resp_1",
        model="gpt-4o",
        content=None,
        tool_calls=[
            ToolCall(
                id="call_ksearch_01",
                function=FunctionCall(name="knowledge_search", arguments='{"query": "authentication"}'),
            )
        ],
        finish_reason="tool_calls",
    )
    # Turn 2: LLM produces final answer
    resp2 = CanonicalLLMResponse(
        id="resp_2",
        model="gpt-4o",
        content="The system uses Bearer tokens for authentication.",
        finish_reason="stop",
    )

    llm_client = MockLLMClient([resp1, resp2])
    orchestrator = ChatOrchestratorService(
        llm_client=llm_client,
        knowledge_service=mock_ks,
    )

    request = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="How do we authenticate?")],
    )

    result = await orchestrator.orchestrate_chat(request, workspace_id="global")

    assert result.finish_reason == "stop"
    assert len(result.content) == 1
    assert isinstance(result.content[0], CanonicalTextBlock)
    assert "Bearer tokens for authentication" in result.content[0].text
    assert llm_client.call_count == 2
    mock_ks.execute_tool.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_provider_conversation_supports_search_only():
    mock_ks = MagicMock(spec=KnowledgeService)
    mock_ks.execute_tool = AsyncMock(side_effect=[
        ToolResult(
            tool_call_id="call_1",
            name="knowledge_search",
            content=json.dumps({"results": [], "count": 0}),
            is_error=False,
        ),
    ])

    resp1 = CanonicalLLMResponse(
        id="resp_1",
        model="gpt-4o",
        tool_calls=[
            ToolCall(
                id="call_1",
                function=FunctionCall(name="knowledge_search", arguments='{"query": "logging standard"}'),
            )
        ],
        finish_reason="tool_calls",
    )
    resp2 = CanonicalLLMResponse(
        id="resp_2",
        model="gpt-4o",
        content="Saved the new logging standard.",
        finish_reason="stop",
    )

    llm_client = MockLLMClient([resp1, resp2])
    orchestrator = ChatOrchestratorService(
        llm_client=llm_client,
        knowledge_service=mock_ks,
    )

    request = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="Create logging standard.")],
    )

    result = await orchestrator.orchestrate_chat(request, workspace_id="ws_1")

    assert result.finish_reason == "stop"
    assert "Saved the new logging standard." in result.content[0].text
    assert llm_client.call_count == 2
    assert mock_ks.execute_tool.await_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_tool_passthrough_immediate_exit():
    """Verify external harness tool call (e.g. bash) halts the loop immediately and returns to client."""
    mock_ks = MagicMock(spec=KnowledgeService)

    resp1 = CanonicalLLMResponse(
        id="resp_ext",
        model="gpt-4o",
        content="I will run the tests.",
        tool_calls=[
            ToolCall(
                id="call_bash_01",
                function=FunctionCall(name="bash", arguments='{"command": "pytest tests/ -v"}'),
            )
        ],
        finish_reason="tool_calls",
    )

    llm_client = MockLLMClient([resp1])
    orchestrator = ChatOrchestratorService(
        llm_client=llm_client,
        knowledge_service=mock_ks,
    )

    request = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="Run the test suite.")],
    )

    result = await orchestrator.orchestrate_chat(request)

    assert result.finish_reason == "tool_use"
    # Content has text block and tool_use block
    tool_uses = [b for b in result.content if isinstance(b, CanonicalToolUseBlock)]
    assert len(tool_uses) == 1
    assert tool_uses[0].name == "bash"
    assert tool_uses[0].input["command"] == "pytest tests/ -v"
    assert llm_client.call_count == 1
    mock_ks.execute_tool.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_max_tool_iterations_guardrail_terminates_infinite_loop():
    """Verify max_tool_iterations limit stops runaway recursive internal tool loops."""
    mock_ks = MagicMock(spec=KnowledgeService)
    mock_ks.execute_tool = AsyncMock(return_value=ToolResult(
        tool_call_id="call_loop",
        name="knowledge_search",
        content=json.dumps({"results": [], "count": 0}),
        is_error=False,
    ))

    # Queue 10 repetitive internal tool calls
    responses = [
        CanonicalLLMResponse(
            id=f"resp_{i}",
            model="gpt-4o",
            tool_calls=[
                ToolCall(
                    id=f"call_{i}",
                    function=FunctionCall(name="knowledge_search", arguments=f'{{"query": "loop {i}"}}'),
                )
            ],
            finish_reason="tool_calls",
        )
        for i in range(10)
    ]

    llm_client = MockLLMClient(responses)
    orchestrator = ChatOrchestratorService(
        llm_client=llm_client,
        knowledge_service=mock_ks,
        max_tool_iterations=4,
    )

    request = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="Start loop.")],
    )

    with pytest.raises(ToolExecutionException):
        await orchestrator.orchestrate_chat(request)
    assert llm_client.call_count == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_internal_tool_execution_with_retrieval_service():
    """Verify knowledge_search utilizes RetrievalService hybrid search when available."""
    mock_retrieval = MagicMock(spec=IRetrievalService)
    mock_retrieval.hybrid_search = AsyncMock(return_value=[
        BlendedSearchResult(
            id="doc-123",
            source_type="document_chunk",
            title="architecture.md",
            content="Clean Architecture principles.",
            metadata={"filename": "architecture.md"},
            rrf_score=0.032,
            normalized_score=0.95,
            workspace_id="proj_1",
        )
    ])

    resp1 = CanonicalLLMResponse(
        id="resp_1",
        model="gpt-4o",
        tool_calls=[
            ToolCall(
                id="call_hybrid_01",
                function=FunctionCall(name="knowledge_search", arguments='{"query": "architecture", "limit": 3}'),
            )
        ],
        finish_reason="tool_calls",
    )
    resp2 = CanonicalLLMResponse(
        id="resp_2",
        model="gpt-4o",
        content="Based on architecture.md, Clean Architecture principles apply.",
        finish_reason="stop",
    )

    llm_client = MockLLMClient([resp1, resp2])
    orchestrator = ChatOrchestratorService(
        llm_client=llm_client,
        retrieval_service=mock_retrieval,
    )

    request = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="Search architecture.")],
    )

    result = await orchestrator.orchestrate_chat(request, workspace_id="proj_1")

    assert result.finish_reason == "stop"
    assert "Based on architecture.md" in result.content[0].text
    mock_retrieval.hybrid_search.assert_awaited_once_with(
        query="architecture",
        workspace_id="proj_1",
        limit=3,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unavailable_search_executor_errors_are_fed_back():
    resp1 = CanonicalLLMResponse(
        id="resp_1",
        model="gpt-4o",
        tool_calls=[
            ToolCall(
                id="call_occ_err",
                function=FunctionCall(
                    name="knowledge_search",
                    arguments='{"limit": 99}',
                ),
            )
        ],
        finish_reason="tool_calls",
    )
    resp2 = CanonicalLLMResponse(
        id="resp_2",
        model="gpt-4o",
        content="The search request was invalid.",
        finish_reason="stop",
    )

    llm_client = MockLLMClient([resp1, resp2])
    orchestrator = ChatOrchestratorService(
        llm_client=llm_client,
    )

    request = CanonicalChatRequest(
        model="gpt-4o",
        messages=[CanonicalMessage(role="user", content="Search knowledge.")],
    )

    result = await orchestrator.orchestrate_chat(request)

    assert result.finish_reason == "stop"
    assert llm_client.call_count == 2
    second_turn_messages = llm_client.call_history[1]["messages"]
    tool_msg = next((m for m in second_turn_messages if m["role"] == "tool"), None)
    assert tool_msg is not None
    assert "retrieval_error" in tool_msg["content"]
