from __future__ import annotations

import json
import logging
import time
import uuid
from typing import AsyncIterator, Dict, Union
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
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalMessage,
    CanonicalTextBlock,
    CanonicalThinkingBlock,
    CanonicalToolUseBlock,
)
from src.gateway.domain.exceptions import (
    GatewayException,
    ToolExecutionException,
)
from src.gateway.domain.tools import FunctionDefinition, ToolDefinition
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from src.gateway.infrastructure.runtime_settings_provider import load_active_retrieval_settings, load_active_runtime_policy
from src.gateway.presentation.authorization import require_scope
from src.gateway.presentation.quota_usage import record_token_usage
from src.gateway.presentation.schemas.openai_schemas import (
    OpenAIErrorResponse,
)
from src.gateway.presentation.schemas.responses_schemas import (
    ResponsesFunctionCallItem,
    ResponsesInputMessage,
    ResponsesOutputMessage,
    ResponsesOutputText,
    ResponsesReasoningItem,
    ResponsesRequest,
    ResponsesResponse,
    ResponsesUsage,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI Responses"])

UNSUPPORTED_RESPONSES_FIELDS = (
    "truncation",
    "previous_response_id",
    "parallel_tool_calls",
)


def reject_unsupported_field(field: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": {
                "message": f"Field '{field}' is not supported by this gateway. Omit it or use the equivalent chat completions parameter.",
                "type": "invalid_request_error",
                "param": field,
                "code": "unsupported_field",
            }
        },
    )


def responses_request_to_canonical(request: ResponsesRequest) -> CanonicalChatRequest:
    messages: list[CanonicalMessage] = []
    system_prompts: list[str] = []
    if request.instructions:
        system_prompts.append(request.instructions)
    if isinstance(request.input, str):
        if request.input.strip():
            messages.append(
                CanonicalMessage(role="user", content=[CanonicalTextBlock(text=request.input)])
            )
    else:
        for item in request.input:
            text = _message_text(item)
            if item.role in ("system", "developer"):
                if text:
                    system_prompts.append(text)
                messages.append(
                    CanonicalMessage(role="system", content=[CanonicalTextBlock(text=text)])
                )
            elif item.role == "assistant":
                blocks = [CanonicalTextBlock(text=text)] if text else []
                messages.append(CanonicalMessage(role="assistant", content=blocks))
            else:
                messages.append(
                    CanonicalMessage(role="user", content=[CanonicalTextBlock(text=text)])
                )
    tools: list[ToolDefinition] = []
    if request.tools:
        for tool in request.tools:
            tools.append(
                ToolDefinition(
                    type="function",
                    function=FunctionDefinition(
                        name=tool.function.name,
                        description=tool.function.description or "",
                        parameters=tool.function.parameters or {},
                    ),
                )
            )
    extra_params: Dict[str, object] = {}
    if request.metadata is not None:
        extra_params["metadata"] = request.metadata
    if request.reasoning is not None:
        extra_params["reasoning"] = request.reasoning.model_dump(exclude_none=True)
    return CanonicalChatRequest(
        model=request.model,
        messages=messages,
        system_prompt="\n".join(system_prompts) if system_prompts else None,
        temperature=request.temperature,
        top_p=request.top_p,
        max_tokens=request.max_output_tokens,
        stream=bool(request.stream),
        tools=tools,
        tool_choice=request.tool_choice,
        workspace_id=request.workspace_id or get_settings().gateway.default_workspace_id,
        extra_params=extra_params,
    )


def _message_text(item: ResponsesInputMessage) -> str:
    if isinstance(item.content, str):
        return item.content
    parts: list[str] = []
    for part in item.content:
        if not isinstance(part, dict):
            raise ValueError("Only text input parts are supported")
        part_type = part.get("type")
        if part_type in ("input_text", "text"):
            text = part.get("text")
            if not isinstance(text, str):
                raise ValueError("Only text input parts are supported")
            parts.append(text)
        else:
            raise ValueError("Only text input parts are supported")
    return "".join(parts)


def canonical_response_to_responses(
    resp: CanonicalChatResponse,
    *,
    response_id: str,
    created: int,
) -> ResponsesResponse:
    text = "".join(
        block.text for block in resp.content if isinstance(block, CanonicalTextBlock)
    )
    thinking = "".join(
        block.thinking for block in resp.content if isinstance(block, CanonicalThinkingBlock)
    )
    output = []
    if thinking:
        output.append(
            ResponsesReasoningItem(content=[ResponsesOutputText(text=thinking)])
        )
    output.append(
        ResponsesOutputMessage(
            content=[ResponsesOutputText(text=text)],
        )
    )
    for block in resp.content:
        if isinstance(block, CanonicalToolUseBlock):
            args = block.input
            output.append(
                ResponsesFunctionCallItem(
                    call_id=block.id,
                    name=block.name,
                    arguments=json.dumps(args, ensure_ascii=False) if isinstance(args, (dict, list)) else str(args),
                )
            )
    return ResponsesResponse(
        id=response_id,
        created=created,
        model=resp.model,
        status="completed",
        output=output,
        usage=ResponsesUsage(
            input_tokens=resp.usage.prompt_tokens,
            output_tokens=resp.usage.completion_tokens,
            total_tokens=resp.usage.total_tokens,
        ),
    )


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
        runtime_policy_provider=load_active_runtime_policy,
    )


@router.post(
    "/responses",
    dependencies=[Depends(require_scope("chat:write"))],
    response_model=None,
    responses={
        200: {"description": "Successful response object or SSE stream"},
        400: {"model": OpenAIErrorResponse},
        401: {"model": OpenAIErrorResponse},
        404: {"model": OpenAIErrorResponse},
        500: {"model": OpenAIErrorResponse},
        502: {"model": OpenAIErrorResponse},
    },
)
async def create_response(
    request: ResponsesRequest,
    http_request: Request,
    orchestrator: IChatOrchestrator = Depends(get_chat_orchestrator),
    model_registry: ModelRegistryService = Depends(get_model_registry),
) -> Union[ResponsesResponse, StreamingResponse, JSONResponse]:
    try:
        for field in UNSUPPORTED_RESPONSES_FIELDS:
            if getattr(request, field) is not None:
                return reject_unsupported_field(field)

        resolved_model = model_registry.resolve(request.model)

        try:
            canonical_req = responses_request_to_canonical(request)
        except ValueError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                        "param": "input",
                        "code": "unsupported_input",
                    }
                },
            )
        canonical_req.model = resolved_model

        if not canonical_req.messages:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "message": "Invalid 'input': expected a non-empty string or at least one message.",
                        "type": "invalid_request_error",
                        "param": "input",
                        "code": "empty_input",
                    }
                },
            )

        if canonical_req.stream:
            return _handle_streaming_response(
                canonical_req=canonical_req,
                orchestrator=orchestrator,
            )

        canonical_resp: CanonicalChatResponse = await orchestrator.orchestrate_chat(
            request=canonical_req,
            workspace_id=canonical_req.workspace_id or get_settings().gateway.default_workspace_id,
        )

        await record_token_usage(
            getattr(http_request.state, "principal", None),
            space_id=canonical_req.workspace_id or get_settings().gateway.default_workspace_id,
            tokens=canonical_resp.usage.total_tokens,
        )
        return canonical_response_to_responses(
            canonical_resp,
            response_id=f"resp_{uuid.uuid4().hex[:12]}",
            created=int(time.time()),
        )

    except GatewayException as exc:
        logger.warning(
            "Gateway responses error",
            extra={"error_type": exc.error_type},
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": exc.message if exc.status_code < 500 else "A gateway dependency failed.",
                    "type": exc.error_type,
                    "code": exc.code,
                }
            },
        )
    except Exception as exc:
        logger.error(
            "Unhandled responses error",
            extra={"exception_class": type(exc).__name__},
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "message": "An internal server error occurred.",
                    "type": "internal_server_error",
                    "code": "internal_error",
                }
            },
        )


def _handle_streaming_response(
    canonical_req: CanonicalChatRequest,
    orchestrator: IChatOrchestrator,
) -> StreamingResponse:
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())
    model_name = canonical_req.model

    async def sse_generator() -> AsyncIterator[str]:
        created_event = {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "created": created_ts,
                "model": model_name,
                "status": "in_progress",
                "output": [],
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            },
        }
        yield f"data: {json.dumps(created_event)}\n\n"

        in_progress_event = {
            "type": "response.in_progress",
            "response": {
                "id": response_id,
                "object": "response",
                "created": created_ts,
                "model": model_name,
                "status": "in_progress",
            },
        }
        yield f"data: {json.dumps(in_progress_event)}\n\n"

        try:
            stream_iter = orchestrator.orchestrate_chat_stream(
                request=canonical_req,
                workspace_id=canonical_req.workspace_id or get_settings().gateway.default_workspace_id,
            )

            async for chunk in stream_iter:
                if chunk.delta_content is not None:
                    yield f"data: {json.dumps({'type': 'response.output_text.delta', 'item_id': 'msg_0', 'output_index': 0, 'content_index': 0, 'delta': chunk.delta_content})}\n\n"
                if chunk.delta_thinking is not None:
                    yield f"data: {json.dumps({'type': 'response.reasoning_summary_text.delta', 'item_id': 'rs_0', 'output_index': 0, 'summary_index': 0, 'delta': chunk.delta_thinking})}\n\n"
                if chunk.delta_tool_calls:
                    for tc in chunk.delta_tool_calls:
                        yield f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'item_id': tc.id or 'fc_0', 'output_index': 0, 'delta': tc.function.arguments})}\n\n"
                if chunk.usage is not None:
                    usage_event = {
                        "type": "response.usage.delta",
                        "response_id": response_id,
                        "usage": {
                            "input_tokens": chunk.usage.prompt_tokens,
                            "output_tokens": chunk.usage.completion_tokens,
                            "total_tokens": chunk.usage.total_tokens,
                        },
                    }
                    yield f"data: {json.dumps(usage_event)}\n\n"

        except GatewayException as exc:
            logger.warning(
                "Responses stream generation terminated",
                extra={"error_type": exc.error_type, "error_code": exc.code},
            )
            tool_error = isinstance(exc, ToolExecutionException)
            err_chunk = {
                "type": "error",
                "error": {
                    "message": exc.message if exc.status_code < 500 else "The response stream ended unexpectedly.",
                    "type": exc.error_type if tool_error else "streaming_error",
                    "code": exc.code if tool_error else "streaming_error",
                },
            }
            yield f"data: {json.dumps(err_chunk)}\n\n"
        except Exception as exc:
            logger.error(
                "Responses stream generation failed",
                extra={"exception_class": type(exc).__name__},
            )
            err_chunk = {
                "error": {
                    "message": "The response stream ended unexpectedly.",
                    "type": "streaming_error",
                    "code": 500,
                }
            }
            yield f"data: {json.dumps(err_chunk)}\n\n"

        completed_event = {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "created": created_ts,
                "model": model_name,
                "status": "completed",
            },
        }
        yield f"data: {json.dumps(completed_event)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
