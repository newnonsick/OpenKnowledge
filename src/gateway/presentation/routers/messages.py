

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Union
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from src.gateway.config import get_settings
from src.gateway.application.ports.clients import IEmbeddingClient, ILLMClient
from src.gateway.application.services.chat_orchestrator import (
    ChatOrchestratorService,
    IChatOrchestrator,
)
from src.gateway.application.services.authorized_retrieval_service import AuthorizedRetrievalService
from src.gateway.application.services.model_registry import (
    ModelRegistryService,
    get_model_registry,
)
from src.gateway.domain.canonical import (
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
from src.gateway.domain.exceptions import (
    AuthenticationException,
    GatewayException,
    LLMProviderException,
    ModelNotFoundException,
    ToolExecutionException,
)
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from src.gateway.infrastructure.runtime_settings_provider import load_active_retrieval_settings
from src.gateway.presentation.converters.anthropic_converter import (
    anthropic_request_to_canonical,
    canonical_response_to_anthropic,
)
from src.gateway.presentation.authorization import require_scope
from src.gateway.presentation.schemas.anthropic_schemas import (
    AnthropicContentBlock,
    AnthropicContentBlockDeltaEvent,
    AnthropicContentBlockStartEvent,
    AnthropicContentBlockStopEvent,
    AnthropicErrorDetail,
    AnthropicErrorResponse,
    AnthropicMessageDeltaBody,
    AnthropicMessageDeltaEvent,
    AnthropicMessageDeltaUsage,
    AnthropicMessageParam,
    AnthropicMessageStartEvent,
    AnthropicMessageStopEvent,
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    AnthropicTextBlock,
    AnthropicTextDelta,
    AnthropicToolParam,
    AnthropicToolResultBlock,
    AnthropicToolUseBlock,
    AnthropicUsage,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Anthropic Messages"])

def _canonical_to_upstream_payload(
    canonical_req: CanonicalChatRequest,
) -> tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:

    upstream_msgs: List[Dict[str, Any]] = []

    has_system_msg = any(m.role == "system" for m in canonical_req.messages)
    if canonical_req.system_prompt and not has_system_msg:
        upstream_msgs.append({"role": "system", "content": canonical_req.system_prompt})

    for msg in canonical_req.messages:
        if msg.role == "system":
            upstream_msgs.append({"role": "system", "content": msg.text_content})
        elif msg.role == "user":
            upstream_msgs.append({"role": "user", "content": msg.text_content})
        elif msg.role == "assistant":
            msg_dict: Dict[str, Any] = {"role": "assistant"}
            text = msg.text_content
            if text:
                msg_dict["content"] = text
            else:
                msg_dict["content"] = None

            tool_uses = msg.tool_uses
            if tool_uses:
                tc_list = []
                for tu in tool_uses:
                    tc_list.append({
                        "id": tu.id,
                        "type": "function",
                        "function": {
                            "name": tu.name,
                            "arguments": json.dumps(tu.input, ensure_ascii=False) if isinstance(tu.input, (dict, list)) else str(tu.input),
                        },
                    })
                msg_dict["tool_calls"] = tc_list
            upstream_msgs.append(msg_dict)
        elif msg.role == "tool":
            for tr in msg.tool_results:
                content_str = tr.content if isinstance(tr.content, str) else json.dumps(tr.content)
                upstream_msgs.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_use_id,
                    "content": content_str,
                })

    upstream_tools = None
    if canonical_req.tools:
        upstream_tools = []
        for t in canonical_req.tools:
            upstream_tools.append({
                "type": "function",
                "function": {
                    "name": t.function.name,
                    "description": t.function.description,
                    "parameters": t.function.parameters,
                },
            })

    return upstream_msgs, upstream_tools

def get_llm_client() -> ILLMClient:

    return HttpLLMClient()

def get_embedding_client() -> IEmbeddingClient:

    return HTTPEmbeddingClient()

def get_retrieval_service(
    embedding_client: IEmbeddingClient = Depends(get_embedding_client),
) -> AuthorizedRetrievalService:

    return AuthorizedRetrievalService(
        PostgresRetrievalUnitRepository(),
        embedding_client,
        runtime_settings_provider=load_active_retrieval_settings,
    )

def get_chat_orchestrator(
    llm_client: ILLMClient = Depends(get_llm_client),
    retrieval_service: AuthorizedRetrievalService = Depends(get_retrieval_service),
) -> IChatOrchestrator:

    return ChatOrchestratorService(
        llm_client=llm_client,
        retrieval_service=retrieval_service,
    )

@router.post(
    "/messages",
    dependencies=[Depends(require_scope("chat:write"))],
    response_model=None,
    responses={
        200: {"description": "Successful message completion or SSE stream"},
        400: {"model": AnthropicErrorResponse},
        401: {"model": AnthropicErrorResponse},
        404: {"model": AnthropicErrorResponse},
        500: {"model": AnthropicErrorResponse},
        502: {"model": AnthropicErrorResponse},
    },
)
async def create_message(
    request: AnthropicMessagesRequest,
    orchestrator: IChatOrchestrator = Depends(get_chat_orchestrator),
    model_registry: ModelRegistryService = Depends(get_model_registry),
) -> Union[AnthropicMessagesResponse, StreamingResponse, JSONResponse]:

    try:

        if not request.messages:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "messages: Input should be a valid list with at least 1 item.",
                    },
                },
            )

        resolved_model = model_registry.resolve(request.model)

        canonical_req = anthropic_request_to_canonical(request)
        canonical_req.model = resolved_model

        if canonical_req.stream:
            return _handle_anthropic_streaming(
                canonical_req=canonical_req,
                orchestrator=orchestrator,
            )

        canonical_resp: CanonicalChatResponse = await orchestrator.orchestrate_chat(
            request=canonical_req,
            workspace_id=canonical_req.workspace_id or get_settings().gateway.default_workspace_id,
        )

        anthropic_resp = canonical_response_to_anthropic(canonical_resp)
        return anthropic_resp

    except GatewayException as exc:
        logger.warning(
            "Gateway Anthropic message error",
            extra={"error_type": exc.error_type},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "type": "error",
                "error": {
                    "type": exc.error_type,
                    "message": exc.message if exc.status_code < 500 else "A gateway dependency failed.",
                },
            },
        )
    except Exception as exc:
        logger.error(
            "Unhandled Anthropic message error",
            extra={"exception_class": type(exc).__name__},
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "An internal server error occurred.",
                },
            },
        )

def _handle_anthropic_streaming(
    canonical_req: CanonicalChatRequest,
    orchestrator: IChatOrchestrator,
) -> StreamingResponse:

    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    model_name = canonical_req.model

    def sse(event_name: str, payload: Dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"

    async def sse_generator() -> AsyncIterator[str]:

        message_start_payload = {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model_name,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
        yield sse("message_start", message_start_payload)

        next_index = 0
        open_block: Optional[str] = None
        text_block_opened = False
        has_tool_use = False
        output_tokens = 0

        real_input_tokens: Optional[int] = None
        real_output_tokens: Optional[int] = None

        open_tool_blocks: Dict[Any, int] = {}
        tool_block_ids: Dict[Any, str] = {}
        final_finish_reason: Optional[str] = None

        def block_start(block_type: str) -> str:
            nonlocal next_index, open_block, text_block_opened
            open_block = block_type
            if block_type == "text":
                text_block_opened = True
                content_block: Dict[str, Any] = {"type": "text", "text": ""}
            else:
                content_block = {"type": "thinking", "thinking": ""}
            return sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": next_index,
                    "content_block": content_block,
                },
            )

        def block_stop() -> str:
            nonlocal next_index, open_block
            event = sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": next_index},
            )
            next_index += 1
            open_block = None
            return event

        try:
            stream_iter = orchestrator.orchestrate_chat_stream(
                request=canonical_req,
                workspace_id=canonical_req.workspace_id or get_settings().gateway.default_workspace_id,
            )

            async for chunk in stream_iter:
                if chunk.finish_reason:
                    final_finish_reason = chunk.finish_reason

                if chunk.usage:
                    real_input_tokens = (real_input_tokens or 0) + chunk.usage.prompt_tokens
                    real_output_tokens = (real_output_tokens or 0) + chunk.usage.completion_tokens

                if chunk.delta_thinking:
                    if open_block != "thinking":
                        if open_block is not None:
                            yield block_stop()
                        yield block_start("thinking")
                    yield sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": next_index,
                            "delta": {"type": "thinking_delta", "thinking": chunk.delta_thinking},
                        },
                    )
                    output_tokens += max(1, len(chunk.delta_thinking) // 4)

                if chunk.delta_content:
                    if open_block != "text":
                        if open_block is not None:
                            yield block_stop()
                        yield block_start("text")
                    yield sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": next_index,
                            "delta": {"type": "text_delta", "text": chunk.delta_content},
                        },
                    )
                    output_tokens += max(1, len(chunk.delta_content) // 4)

                if chunk.delta_tool_calls:
                    has_tool_use = True

                    if open_block is not None:
                        yield block_stop()

                    if not text_block_opened and not open_tool_blocks:
                        yield block_start("text")
                        yield block_stop()

                    for pos, tc in enumerate(chunk.delta_tool_calls):
                        slot = tc.index if tc.index is not None else pos
                        if slot not in open_tool_blocks:
                            block_id = tc.id or tool_block_ids.get(slot) or f"toolu_{uuid.uuid4().hex[:12]}"
                            tool_block_ids[slot] = block_id
                            open_tool_blocks[slot] = next_index
                            yield sse(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": next_index,
                                    "content_block": {
                                        "type": "tool_use",
                                        "id": block_id,
                                        "name": tc.function.name or "",
                                        "input": {},
                                    },
                                },
                            )
                            next_index += 1

                        if tc.function.arguments:
                            yield sse(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": open_tool_blocks[slot],
                                    "delta": {
                                        "type": "input_json_delta",
                                        "partial_json": tc.function.arguments,
                                    },
                                },
                            )

        except GatewayException as exc:
            logger.warning(
                "Anthropic stream generation terminated",
                extra={"error_type": exc.error_type, "error_code": exc.code},
            )
            tool_error = isinstance(exc, ToolExecutionException)
            err_event = {
                "type": "error",
                "error": {
                    "type": exc.code if tool_error else "api_error",
                    "message": exc.message if exc.status_code < 500 else "The response stream ended unexpectedly.",
                },
            }
            yield sse("error", err_event)
            return
        except Exception as exc:
            logger.error(
                "Anthropic stream generation failed",
                extra={"exception_class": type(exc).__name__},
            )
            err_event = {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "The response stream ended unexpectedly.",
                },
            }
            yield sse("error", err_event)
            return

        if open_block is None and not text_block_opened and not open_tool_blocks:
            yield block_start("text")
        if open_block is not None:
            yield block_stop()

        for block_idx in open_tool_blocks.values():
            yield sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})
        open_tool_blocks.clear()

        if final_finish_reason == "max_tokens":
            stop_reason = "max_tokens"
        elif has_tool_use:
            stop_reason = "tool_use"
        elif final_finish_reason == "content_filter":
            stop_reason = "refusal"
        else:
            stop_reason = "end_turn"
        message_delta_payload = {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {
                "output_tokens": real_output_tokens if real_output_tokens is not None else output_tokens,
                "input_tokens": real_input_tokens if real_input_tokens is not None else 0,
            },
        }
        yield sse("message_delta", message_delta_payload)

        yield sse("message_stop", {"type": "message_stop"})

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
