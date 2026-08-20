from __future__ import annotations

import json
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from src.gateway.application.services.chat_orchestrator import ChatOrchestratorService
from src.gateway.application.services.knowledge_service import KnowledgeService
from src.gateway.application.services.model_registry import ModelRegistryService
from src.gateway.domain.canonical import CanonicalChatRequest, CanonicalChatResponse, CanonicalLLMResponse, CanonicalLLMStreamChunk, CanonicalMessage, CanonicalTextBlock, CanonicalThinkingBlock
from src.gateway.domain.exceptions import ModelNotFoundException, ToolExecutionException, ValidationException
from src.gateway.domain.tools import FunctionCall, FunctionDefinition, ToolCall, ToolDefinition, ToolResult
from src.gateway.presentation.converters.anthropic_converter import canonical_response_to_anthropic
from src.gateway.presentation.routers.messages import _handle_anthropic_streaming
from src.gateway.presentation.schemas.anthropic_schemas import AnthropicMessagesRequest
from src.gateway.presentation.schemas.openai_schemas import OpenAIChatCompletionRequest


class ScriptedClient:
    def __init__(self, responses=None, streams=None) -> None:
        self.responses = list(responses or [])
        self.streams = list(streams or [])

    async def generate(self, **kwargs):
        return self.responses.pop(0)

    async def generate_stream(self, **kwargs):
        for chunk in self.streams.pop(0):
            yield chunk


def request() -> CanonicalChatRequest:
    return CanonicalChatRequest(
        model="model",
        messages=[CanonicalMessage(role="user", content="hello")],
    )


def call(name: str, call_id: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        function=FunctionCall(name=name, arguments='{"query":"x"}'),
    )


def test_unknown_models_fail_closed_and_explicit_aliases_still_resolve():
    registry = ModelRegistryService(
        default_model="backend",
        aliases={"coding": "backend"},
    )

    assert registry.resolve("CODING") == "backend"
    with pytest.raises(ModelNotFoundException):
        registry.resolve("invented-model")
    with pytest.raises(ModelNotFoundException):
        registry.get_model_info("invented-model")


def test_anthropic_thinking_signature_is_preserved_or_omitted_never_fabricated():
    unsigned = canonical_response_to_anthropic(
        CanonicalChatResponse(
            id="unsigned",
            model="model",
            content=[CanonicalThinkingBlock(thinking="thought")],
        )
    )
    signed = canonical_response_to_anthropic(
        CanonicalChatResponse(
            id="signed",
            model="model",
            content=[CanonicalThinkingBlock(thinking="thought", signature="opaque-provider-value")],
        )
    )

    unsigned_block = unsigned.content[0]
    signed_block = signed.content[0]
    unsigned_payload = unsigned_block if isinstance(unsigned_block, dict) else unsigned_block.model_dump()
    signed_payload = signed_block if isinstance(signed_block, dict) else signed_block.model_dump()
    assert "signature" not in unsigned_payload
    assert signed_payload["signature"] == "opaque-provider-value"


def test_unsupported_media_and_unknown_request_fields_are_rejected_explicitly():
    with pytest.raises(ValidationError):
        OpenAIChatCompletionRequest.model_validate(
            {
                "model": "model",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
                        ],
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        AnthropicMessagesRequest.model_validate(
            {
                "model": "model",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "AA==",
                                },
                            }
                        ],
                    }
                ],
            }
        )
    with pytest.raises(ValidationError):
        OpenAIChatCompletionRequest.model_validate(
            {
                "model": "model",
                "messages": [{"role": "user", "content": "hello"}],
                "response_format": {"type": "json_object"},
            }
        )


@pytest.mark.asyncio
async def test_mixed_internal_and_external_tool_turn_fails_without_exposure_or_execution():
    client = ScriptedClient(
        responses=[
            CanonicalLLMResponse(
                id="mixed",
                model="model",
                tool_calls=[call("knowledge_search", "internal"), call("bash", "external")],
                finish_reason="tool_calls",
            )
        ]
    )
    knowledge = MagicMock(spec=KnowledgeService)
    knowledge.execute_tool = AsyncMock()
    service = ChatOrchestratorService(client, knowledge_service=knowledge)

    with pytest.raises(ToolExecutionException):
        await service.orchestrate_chat(request())
    knowledge.execute_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_cannot_shadow_reserved_internal_tool_names():
    service = ChatOrchestratorService(ScriptedClient())
    reserved_request = request().model_copy(
        update={
            "tools": [
                ToolDefinition(
                    function=FunctionDefinition(
                        name="knowledge_search",
                        description="reserved",
                        parameters={"type": "object"},
                    )
                )
            ]
        }
    )

    with pytest.raises(ValidationException):
        await service.orchestrate_chat(reserved_request)


@pytest.mark.asyncio
async def test_stream_buffers_hidden_round_before_classification():
    client = ScriptedClient(
        streams=[
            [
                CanonicalLLMStreamChunk(id="hidden", model="model", delta_content="must-not-leak"),
                CanonicalLLMStreamChunk(
                    id="hidden",
                    model="model",
                    delta_tool_calls=[call("knowledge_search", "internal")],
                    finish_reason="tool_calls",
                ),
            ],
            [
                CanonicalLLMStreamChunk(id="public", model="model", delta_content="final"),
                CanonicalLLMStreamChunk(id="public", model="model", finish_reason="stop"),
            ],
        ]
    )
    knowledge = MagicMock(spec=KnowledgeService)
    knowledge.execute_tool = AsyncMock(
        return_value=ToolResult(
            tool_call_id="internal",
            name="knowledge_search",
            content=json.dumps({"results": []}),
        )
    )
    service = ChatOrchestratorService(client, knowledge_service=knowledge)

    chunks = [chunk async for chunk in service.orchestrate_chat_stream(request())]

    assert "".join(chunk.delta_content or "" for chunk in chunks) == "final"
    assert all(chunk.id == "public" for chunk in chunks)


@pytest.mark.asyncio
async def test_tool_iteration_exhaustion_is_a_gateway_error_not_token_exhaustion():
    response_client = ScriptedClient(
        responses=[
            CanonicalLLMResponse(
                id="loop",
                model="model",
                tool_calls=[call("knowledge_search", "internal")],
                finish_reason="tool_calls",
            )
        ]
    )
    stream_client = ScriptedClient(
        streams=[
            [
                CanonicalLLMStreamChunk(
                    id="loop",
                    model="model",
                    delta_tool_calls=[call("knowledge_search", "internal")],
                    finish_reason="tool_calls",
                )
            ]
        ]
    )
    knowledge = MagicMock(spec=KnowledgeService)
    knowledge.execute_tool = AsyncMock(
        return_value=ToolResult(
            tool_call_id="internal",
            name="knowledge_search",
            content="{}",
        )
    )

    with pytest.raises(ToolExecutionException):
        await ChatOrchestratorService(
            response_client,
            knowledge_service=knowledge,
            max_tool_iterations=1,
        ).orchestrate_chat(request())
    with pytest.raises(ToolExecutionException):
        async for _ in ChatOrchestratorService(
            stream_client,
            knowledge_service=knowledge,
            max_tool_iterations=1,
        ).orchestrate_chat_stream(request()):
            pass


@pytest.mark.asyncio
async def test_internal_tool_timeout_and_hidden_turn_buffer_are_bounded():
    timeout_client = ScriptedClient(
        responses=[
            CanonicalLLMResponse(
                id="slow",
                model="model",
                tool_calls=[call("knowledge_search", "internal")],
                finish_reason="tool_calls",
            )
        ]
    )
    knowledge = MagicMock(spec=KnowledgeService)

    async def slow_tool(**kwargs):
        await asyncio.sleep(1)

    knowledge.execute_tool = AsyncMock(side_effect=slow_tool)
    timeout_service = ChatOrchestratorService(
        timeout_client,
        knowledge_service=knowledge,
        tool_timeout_seconds=0.001,
    )
    buffer_service = ChatOrchestratorService(
        ScriptedClient(
            streams=[
                [
                    CanonicalLLMStreamChunk(
                        id="large",
                        model="model",
                        delta_content="oversized",
                    )
                ]
            ]
        ),
        max_hidden_turn_bytes=4,
    )

    with pytest.raises(ToolExecutionException, match="timed out"):
        await timeout_service.orchestrate_chat(request())
    with pytest.raises(ToolExecutionException, match="buffering budget"):
        async for _ in buffer_service.orchestrate_chat_stream(request()):
            pass


@pytest.mark.asyncio
async def test_anthropic_stream_error_is_terminal():
    class FailingOrchestrator:
        async def orchestrate_chat_stream(self, request, workspace_id):
            if False:
                yield None
            raise RuntimeError("private failure")

    response = _handle_anthropic_streaming(request(), FailingOrchestrator())
    parts = [
        part.decode() if isinstance(part, bytes) else part
        async for part in response.body_iterator
    ]
    body = "".join(parts)
    events = [line.removeprefix("event: ") for line in body.splitlines() if line.startswith("event: ")]

    assert events == ["message_start", "error"]
    assert "private failure" not in body
