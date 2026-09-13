from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Union
from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

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
)
from src.gateway.domain.exceptions import (
    GatewayException,
)
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
    ResponsesInputMessage,
    ResponsesOutputMessage,
    ResponsesOutputText,
    ResponsesRequest,
    ResponsesResponse,
    ResponsesUsage,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI Responses"])


def responses_request_to_canonical(request: ResponsesRequest) -> CanonicalChatRequest:
    messages: list[CanonicalMessage] = []
    system_prompts: list[str] = []
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
    return CanonicalChatRequest(
        model=request.model,
        messages=messages,
        system_prompt="\n".join(system_prompts) if system_prompts else None,
        max_tokens=request.max_output_tokens,
        stream=False,
        workspace_id=request.workspace_id or get_settings().gateway.default_workspace_id,
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
    return ResponsesResponse(
        id=response_id,
        created=created,
        model=resp.model,
        status="completed",
        output=[
            ResponsesOutputMessage(
                content=[ResponsesOutputText(text=text)],
            )
        ],
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
        200: {"description": "Successful response object"},
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
) -> Union[ResponsesResponse, JSONResponse]:

    try:
        if request.stream:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "message": "Streaming responses are not supported. Resubmit with stream=false or omit stream.",
                        "type": "invalid_request_error",
                        "param": "stream",
                        "code": "unsupported_stream",
                    }
                },
            )

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
