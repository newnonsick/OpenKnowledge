

from __future__ import annotations

import json
import logging
import time
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
from src.gateway.application.services.knowledge_service import KnowledgeService
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
)
from src.gateway.domain.tools import ToolCall
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient
from src.gateway.infrastructure.persistence.knowledge_repository import KnowledgeRepository
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from src.gateway.presentation.converters.openai_converter import (
    canonical_response_to_openai,
    canonical_stream_chunk_to_openai,
    map_finish_reason_to_openai,
    openai_request_to_canonical,
)
from src.gateway.presentation.authorization import require_scope
from src.gateway.presentation.schemas.openai_schemas import (
    OpenAIChatCompletionChunk,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
    OpenAIErrorDetail,
    OpenAIErrorResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI Chat Completions"])

def _canonical_to_upstream_messages(canonical_req: CanonicalChatRequest) -> List[Dict[str, Any]]:

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

    return upstream_msgs

def _canonical_to_upstream_tools(canonical_req: CanonicalChatRequest) -> Optional[List[Dict[str, Any]]]:

    if not canonical_req.tools:
        return None
    tools_list = []
    for t in canonical_req.tools:
        tools_list.append({
            "type": "function",
            "function": {
                "name": t.function.name,
                "description": t.function.description,
                "parameters": t.function.parameters,
            },
        })
    return tools_list

def get_llm_client() -> ILLMClient:

    return HttpLLMClient()

def get_embedding_client() -> IEmbeddingClient:

    return HTTPEmbeddingClient()

def get_knowledge_service(
    embedding_client: IEmbeddingClient = Depends(get_embedding_client),
) -> KnowledgeService:

    repo = KnowledgeRepository()
    return KnowledgeService(repository=repo, embedding_client=embedding_client)

def get_retrieval_service(
    embedding_client: IEmbeddingClient = Depends(get_embedding_client),
) -> AuthorizedRetrievalService:

    return AuthorizedRetrievalService(
        PostgresRetrievalUnitRepository(),
        embedding_client,
    )

def get_chat_orchestrator(
    llm_client: ILLMClient = Depends(get_llm_client),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
    retrieval_service: AuthorizedRetrievalService = Depends(get_retrieval_service),
) -> IChatOrchestrator:

    return ChatOrchestratorService(
        llm_client=llm_client,
        knowledge_service=knowledge_service,
        retrieval_service=retrieval_service,
    )

@router.post(
    "/chat/completions",
    dependencies=[Depends(require_scope("chat:write"))],
    response_model=None,
    responses={
        200: {"description": "Successful chat completion or SSE stream"},
        400: {"model": OpenAIErrorResponse},
        401: {"model": OpenAIErrorResponse},
        404: {"model": OpenAIErrorResponse},
        500: {"model": OpenAIErrorResponse},
        502: {"model": OpenAIErrorResponse},
    },
)
async def create_chat_completion(
    request: OpenAIChatCompletionRequest,
    orchestrator: IChatOrchestrator = Depends(get_chat_orchestrator),
    model_registry: ModelRegistryService = Depends(get_model_registry),
) -> Union[OpenAIChatCompletionResponse, StreamingResponse, JSONResponse]:

    try:

        if not request.messages:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "message": "Invalid 'messages': empty array. Expected an array with minimum length 1.",
                        "type": "invalid_request_error",
                        "param": "messages",
                        "code": "array_min_length",
                    }
                },
            )

        resolved_model = model_registry.resolve(request.model)

        canonical_req = openai_request_to_canonical(request)
        canonical_req.model = resolved_model

        if canonical_req.stream:
            return _handle_streaming_completion(
                canonical_req=canonical_req,
                orchestrator=orchestrator,
            )

        canonical_resp: CanonicalChatResponse = await orchestrator.orchestrate_chat(
            request=canonical_req,
            workspace_id=canonical_req.workspace_id or get_settings().gateway.default_workspace_id,
        )

        openai_resp = canonical_response_to_openai(canonical_resp)
        return openai_resp

    except GatewayException as exc:
        logger.warning(
            "Gateway chat completion error",
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
            "Unhandled chat completion error",
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

def _handle_streaming_completion(
    canonical_req: CanonicalChatRequest,
    orchestrator: IChatOrchestrator,
) -> StreamingResponse:

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())
    model_name = canonical_req.model

    async def sse_generator() -> AsyncIterator[str]:

        initial_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(initial_chunk)}\n\n"

        try:
            stream_iter = orchestrator.orchestrate_chat_stream(
                request=canonical_req,
                workspace_id=canonical_req.workspace_id or get_settings().gateway.default_workspace_id,
            )

            async for chunk in stream_iter:
                delta_dict: Dict[str, Any] = {}
                if chunk.delta_content is not None:
                    delta_dict["content"] = chunk.delta_content
                if chunk.delta_thinking is not None:
                    delta_dict["reasoning_content"] = chunk.delta_thinking
                if chunk.delta_tool_calls:
                    tc_list = []
                    for idx, tc in enumerate(chunk.delta_tool_calls):
                        tc_entry: Dict[str, Any] = {
                            "index": tc.index if tc.index is not None else idx,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        if tc.id:
                            tc_entry["id"] = tc.id
                        tc_list.append(tc_entry)
                    delta_dict["tool_calls"] = tc_list

                finish_reason = map_finish_reason_to_openai(chunk.finish_reason)

                is_usage_only = (
                    chunk.usage is not None and not delta_dict and finish_reason is None
                )

                chunk_payload = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model_name,
                    "choices": []
                    if is_usage_only
                    else [
                        {
                            "index": 0,
                            "delta": delta_dict,
                            "finish_reason": finish_reason,
                        }
                    ],
                }
                if chunk.usage:
                    chunk_payload["usage"] = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                        "total_tokens": chunk.usage.total_tokens,
                    }
                yield f"data: {json.dumps(chunk_payload)}\n\n"

        except Exception as exc:
            logger.error(
                "Streaming generation failed",
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
