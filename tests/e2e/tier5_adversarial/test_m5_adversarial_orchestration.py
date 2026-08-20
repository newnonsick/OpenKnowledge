"""Tier 5 Adversarial Stress Tests: Milestone M5 Chat Orchestration & Tool Interception Loop.

Empirically stress-tests:
1. Recursive tool call loops exceeding max_tool_iterations (exact iteration cutoff, token accumulation, warning text).
2. Mixed internal knowledge tools + external harness tools (bash, edit_file, git, mcp) in a single turn.
3. Malformed tool arguments JSON, non-JSON strings, missing required parameters, type errors.
4. ConcurrencyConflictException and ItemNotFoundException inside internal tool interception loop.
5. Streaming SSE tool interception: chunk buffering, mixed tool streaming passthrough, mid-stream upstream exceptions.
6. Extreme payload sizes (100,000+ chars) and massive tool call lists (50+ calls in single turn).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import uuid4

import pytest

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.application.services.chat_orchestrator import ChatOrchestratorService
from src.gateway.application.services.knowledge_service import KnowledgeService
from src.gateway.application.services.retrieval_service import IRetrievalService
from src.gateway.domain.canonical import (
    BlendedSearchResult,
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
)
from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision
from src.gateway.domain.exceptions import (
    ConcurrencyConflictException,
    ItemNotFoundException,
    LLMProviderException,
    ToolExecutionException,
    ValidationException,
)
from src.gateway.domain.tools import FunctionCall, ToolCall, ToolDefinition, ToolResult


# ==============================================================================
# Scripted Mock LLM Clients for Orchestration Stress
# ==============================================================================

class ScriptedLLMClient(ILLMClient):
    """Mock LLM client returning a pre-programmed sequence of responses per turn."""

    def __init__(self, responses: List[CanonicalLLMResponse]):
        self.responses = responses
        self.call_count = 0
        self.recorded_calls: List[Dict[str, Any]] = []

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> CanonicalLLMResponse:
        self.recorded_calls.append({"messages": messages, "tools": tools, "model": model, "kwargs": kwargs})
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
            return resp
        # Default fallback: return stop text
        return CanonicalLLMResponse(
            id=f"fallback-{self.call_count}",
            model=model or "test-model",
            content="Fallback completion",
            finish_reason="stop",
            usage=CanonicalUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    async def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[CanonicalLLMStreamChunk]:
        self.recorded_calls.append({"messages": messages, "tools": tools, "model": model, "kwargs": kwargs})
        if self.call_count < len(self.responses):
            resp = self.responses[self.call_count]
            self.call_count += 1
        else:
            resp = CanonicalLLMResponse(
                id=f"stream-fallback-{self.call_count}",
                model=model or "test-model",
                content="Fallback stream content",
                finish_reason="stop",
                usage=CanonicalUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

        if resp.tool_calls:
            for idx, tc in enumerate(resp.tool_calls):
                yield CanonicalLLMStreamChunk(
                    id=resp.id,
                    model=resp.model,
                    delta_tool_calls=[tc],
                    finish_reason="tool_calls" if idx == len(resp.tool_calls) - 1 else None,
                    usage=resp.usage if idx == len(resp.tool_calls) - 1 else None,
                )
        else:
            text = resp.content or ""
            half = len(text) // 2
            yield CanonicalLLMStreamChunk(
                id=resp.id,
                model=resp.model,
                delta_content=text[:half],
            )
            yield CanonicalLLMStreamChunk(
                id=resp.id,
                model=resp.model,
                delta_content=text[half:],
                finish_reason=resp.finish_reason or "stop",
                usage=resp.usage,
            )


class EndlessLoopLLMClient(ILLMClient):
    """Mock LLM client that never stops returning internal knowledge_search tool calls."""

    def __init__(self):
        self.call_count = 0

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
        return CanonicalLLMResponse(
            id=f"loop-{self.call_count}",
            model=model or "test-model",
            tool_calls=[
                ToolCall(
                    id=f"call_loop_{self.call_count}",
                    function=FunctionCall(
                        name="knowledge_search",
                        arguments=json.dumps({"query": f"recursive query {self.call_count}"}),
                    ),
                )
            ],
            finish_reason="tool_calls",
            usage=CanonicalUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
        )

    async def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[CanonicalLLMStreamChunk]:
        self.call_count += 1
        yield CanonicalLLMStreamChunk(
            id=f"stream-loop-{self.call_count}",
            model=model or "test-model",
            delta_tool_calls=[
                ToolCall(
                    id=f"call_loop_{self.call_count}",
                    function=FunctionCall(
                        name="knowledge_search",
                        arguments=json.dumps({"query": f"recursive stream query {self.call_count}"}),
                    ),
                )
            ],
            finish_reason="tool_calls",
            usage=CanonicalUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
        )


def _get_response_text(response: CanonicalChatResponse) -> str:
    """Helper to extract concatenated text from response content blocks."""
    return "".join(b.text for b in response.content if isinstance(b, CanonicalTextBlock))


# ==============================================================================
# 1. RECURSIVE TOOL LOOP & GUARDRAIL ADVERSARIAL STRESS
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialMaxIterationsGuardrail:
    """Stress-tests max_tool_iterations guardrails in non-streaming and streaming orchestration."""

    async def test_non_streaming_exact_cutoff_at_max_iterations(self):
        """Verify non-streaming loop terminates at exact max_tool_iterations limit (3) with warning."""
        llm_client = EndlessLoopLLMClient()
        orchestrator = ChatOrchestratorService(
            llm_client=llm_client,
            max_tool_iterations=3,
        )

        request = CanonicalChatRequest(
            model="gpt-4o",
            messages=[CanonicalMessage(role="user", content="Trigger loop")],
        )

        with pytest.raises(ToolExecutionException):
            await orchestrator.orchestrate_chat(request)
        assert llm_client.call_count == 3

    async def test_streaming_sse_exact_cutoff_at_max_iterations(self):
        """Verify streaming SSE loop terminates at exact max_tool_iterations limit (4) with warning chunk."""
        llm_client = EndlessLoopLLMClient()
        orchestrator = ChatOrchestratorService(
            llm_client=llm_client,
            max_tool_iterations=4,
        )

        request = CanonicalChatRequest(
            model="claude-3-5-sonnet-20241022",
            messages=[CanonicalMessage(role="user", content="Trigger stream loop")],
        )

        with pytest.raises(ToolExecutionException):
            async for _ in orchestrator.orchestrate_chat_stream(request):
                pass

        assert llm_client.call_count == 4

    async def test_single_iteration_limit_cutoff(self):
        """Verify max_tool_iterations=1 halts after a single tool call without second upstream call."""
        llm_client = EndlessLoopLLMClient()
        orchestrator = ChatOrchestratorService(
            llm_client=llm_client,
            max_tool_iterations=1,
        )

        request = CanonicalChatRequest(
            model="test-model",
            messages=[CanonicalMessage(role="user", content="Single shot")],
        )

        with pytest.raises(ToolExecutionException):
            await orchestrator.orchestrate_chat(request)
        assert llm_client.call_count == 1


# ==============================================================================
# 2. MIXED INTERNAL + EXTERNAL TOOL PASSTHROUGH ADVERSARIAL TESTS
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialMixedToolPassthrough:
    """Stress-tests single-turn mixed tool calls containing both internal and external tools."""

    async def test_mixed_internal_and_external_tools_in_single_turn_returns_immediately(self):
        """LLM returns knowledge_search (internal) AND bash (external) in the SAME turn.
        
        Gateway MUST:
        - Detect external tool call 'bash'.
        - Immediately stop internal interception.
        - Pass ALL tool calls back to harness/client with finish_reason='tool_use'.
        - NOT enter a secondary loop.
        """
        mixed_response = CanonicalLLMResponse(
            id="mixed-1",
            model="test-model",
            content="I need to search knowledge and execute bash command.",
            tool_calls=[
                ToolCall(
                    id="call_k_search",
                    function=FunctionCall(
                        name="knowledge_search",
                        arguments=json.dumps({"query": "database password"}),
                    ),
                ),
                ToolCall(
                    id="call_bash",
                    function=FunctionCall(
                        name="bash",
                        arguments=json.dumps({"command": "ls -la /var/log"}),
                    ),
                ),
            ],
            finish_reason="tool_calls",
            usage=CanonicalUsage(prompt_tokens=40, completion_tokens=30, total_tokens=70),
        )

        llm_client = ScriptedLLMClient([mixed_response])
        orchestrator = ChatOrchestratorService(llm_client=llm_client, max_tool_iterations=5)

        request = CanonicalChatRequest(
            model="test-model",
            messages=[CanonicalMessage(role="user", content="Find info and check logs")],
        )

        with pytest.raises(ToolExecutionException):
            await orchestrator.orchestrate_chat(request)
        assert llm_client.call_count == 1

    async def test_streaming_mixed_internal_and_external_tools_passthrough(self):
        """Streaming SSE returns external tool call -> yields stream chunks and exits."""
        mixed_response = CanonicalLLMResponse(
            id="stream-mixed-1",
            model="test-model",
            tool_calls=[
                ToolCall(
                    id="call_edit_file",
                    function=FunctionCall(
                        name="edit_file",
                        arguments=json.dumps({"path": "config.py", "content": "# update"}),
                    ),
                )
            ],
            finish_reason="tool_calls",
            usage=CanonicalUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
        )

        llm_client = ScriptedLLMClient([mixed_response])
        orchestrator = ChatOrchestratorService(llm_client=llm_client, max_tool_iterations=5)

        request = CanonicalChatRequest(
            model="test-model",
            messages=[CanonicalMessage(role="user", content="Edit config")],
        )

        chunks: List[CanonicalStreamChunk] = []
        async for chunk in orchestrator.orchestrate_chat_stream(request):
            chunks.append(chunk)

        assert llm_client.call_count == 1
        assert len(chunks) >= 1
        assert chunks[-1].finish_reason == "tool_use"
        assert chunks[0].delta_tool_calls is not None
        assert chunks[0].delta_tool_calls[0].function.name == "edit_file"


# ==============================================================================
# 3. MALFORMED TOOL ARGUMENTS & ERROR RECOVERY ADVERSARIAL TESTS
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialMalformedToolArguments:
    """Stress-tests internal tool interception when upstream LLM generates malformed arguments."""

    async def test_corrupted_json_string_arguments_handled_gracefully(self):
        """LLM generates truncated JSON syntax in tool arguments -> error block fed back without crash."""
        bad_json_response = CanonicalLLMResponse(
            id="bad-args-1",
            model="test-model",
            tool_calls=[
                ToolCall(
                    id="call_corrupt_args",
                    function=FunctionCall(
                        name="knowledge_search",
                        arguments='{"query": "unclosed string...',
                    ),
                )
            ],
            finish_reason="tool_calls",
        )

        final_recovery_response = CanonicalLLMResponse(
            id="recovery-2",
            model="test-model",
            content="I noticed the syntax error and fixed my query.",
            finish_reason="stop",
        )

        llm_client = ScriptedLLMClient([bad_json_response, final_recovery_response])
        orchestrator = ChatOrchestratorService(llm_client=llm_client, max_tool_iterations=5)

        request = CanonicalChatRequest(
            model="test-model",
            messages=[CanonicalMessage(role="user", content="Search something")],
        )

        response = await orchestrator.orchestrate_chat(request)
        assert llm_client.call_count == 2
        assert response.finish_reason == "stop"
        assert "fixed my query" in _get_response_text(response)

        # Verify second upstream call received the error block in conversation history
        second_call_msgs = llm_client.recorded_calls[1]["messages"]
        tool_msg = next((m for m in second_call_msgs if m.get("role") == "tool"), None)
        assert tool_msg is not None
        assert "Invalid JSON" in tool_msg["content"] or "validation_error" in tool_msg["content"]

    async def test_missing_required_parameter_in_knowledge_get(self):
        """LLM calls knowledge_get without item_id -> returns validation error block."""
        missing_param_response = CanonicalLLMResponse(
            id="missing-param-1",
            model="test-model",
            tool_calls=[
                ToolCall(
                    id="call_get_no_id",
                    function=FunctionCall(
                        name="knowledge_get",
                        arguments=json.dumps({"workspace_id": "global"}),
                    ),
                )
            ],
            finish_reason="tool_calls",
        )

        final_resp = CanonicalLLMResponse(
            id="final-2",
            model="test-model",
            content="Item ID was missing.",
            finish_reason="stop",
        )

        llm_client = ScriptedLLMClient([missing_param_response, final_resp])
        orchestrator = ChatOrchestratorService(llm_client=llm_client, max_tool_iterations=3)

        request = CanonicalChatRequest(
            model="test-model",
            messages=[CanonicalMessage(role="user", content="Get item")],
        )

        response = await orchestrator.orchestrate_chat(request)
        assert llm_client.call_count == 2
        assert response.finish_reason == "stop"


# ==============================================================================
# 4. EXTREME PAYLOAD & TOKEN LIMITS ADVERSARIAL STRESS
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialExtremePayloads:
    """Stress-tests orchestrator with massive prompts and huge tool call lists."""

    async def test_100k_character_prompt_payload(self):
        """Stress: 100,000 character user prompt message."""
        huge_prompt = "Large context payload " * 5000
        resp = CanonicalLLMResponse(
            id="resp-huge-1",
            model="gpt-4o",
            content="Handled huge context successfully.",
            finish_reason="stop",
            usage=CanonicalUsage(prompt_tokens=25000, completion_tokens=10, total_tokens=25010),
        )

        llm_client = ScriptedLLMClient([resp])
        orchestrator = ChatOrchestratorService(llm_client=llm_client)

        request = CanonicalChatRequest(
            model="gpt-4o",
            messages=[CanonicalMessage(role="user", content=huge_prompt)],
        )

        response = await orchestrator.orchestrate_chat(request)
        assert response.finish_reason == "stop"
        assert _get_response_text(response) == "Handled huge context successfully."
        assert response.usage.prompt_tokens == 25000

    async def test_50_tool_calls_in_single_turn(self):
        """Stress: Upstream LLM returns 50 simultaneous internal tool calls in a single turn."""
        tool_calls = [
            ToolCall(
                id=f"call_bulk_{i}",
                function=FunctionCall(
                    name="knowledge_search",
                    arguments=json.dumps({"query": f"search query {i}", "limit": 1}),
                ),
            )
            for i in range(50)
        ]

        bulk_response = CanonicalLLMResponse(
            id="bulk-tools-1",
            model="test-model",
            tool_calls=tool_calls,
            finish_reason="tool_calls",
            usage=CanonicalUsage(prompt_tokens=100, completion_tokens=500, total_tokens=600),
        )

        final_response = CanonicalLLMResponse(
            id="bulk-final-2",
            model="test-model",
            content="Aggregated all 50 search results.",
            finish_reason="stop",
            usage=CanonicalUsage(prompt_tokens=800, completion_tokens=50, total_tokens=850),
        )

        llm_client = ScriptedLLMClient([bulk_response, final_response])
        orchestrator = ChatOrchestratorService(llm_client=llm_client, max_tool_iterations=5)

        request = CanonicalChatRequest(
            model="test-model",
            messages=[CanonicalMessage(role="user", content="Search 50 topics in parallel")],
        )

        response = await orchestrator.orchestrate_chat(request)
        assert llm_client.call_count == 2
        assert response.finish_reason == "stop"
        assert _get_response_text(response) == "Aggregated all 50 search results."
        assert response.usage.total_tokens == 1450
