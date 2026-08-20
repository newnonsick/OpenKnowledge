

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Union

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.application.services.authorized_retrieval_service import AuthorizedRetrievalService
from src.gateway.application.services.knowledge_service import KnowledgeService
from src.gateway.application.services.retrieval_service import IRetrievalService
from src.gateway.config import get_settings
from src.gateway.domain.canonical import (
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalThinkingBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
)
from src.gateway.domain.exceptions import (
    AuthorizationException,
    ConcurrencyConflictException,
    GatewayException,
    ItemNotFoundException,
    LLMProviderException,
    ToolExecutionException,
    ValidationException,
)
from src.gateway.domain.prompts import compose_system_prompt
from src.gateway.domain.tools import (
    FunctionCall,
    ToolCall,
    ToolDefinition,
    ToolResult,
    get_internal_tool_definitions,
    is_internal_tool,
)
from src.gateway.infrastructure.persistence.principal_context import get_bound_principal

logger = logging.getLogger(__name__)

class IChatOrchestrator(ABC):

    @abstractmethod
    async def orchestrate_chat(
        self,
        request: CanonicalChatRequest,
        workspace_id: str = get_settings().gateway.default_workspace_id,
    ) -> CanonicalChatResponse:

        ...

    @abstractmethod
    async def orchestrate_chat_stream(
        self,
        request: CanonicalChatRequest,
        workspace_id: str = get_settings().gateway.default_workspace_id,
    ) -> AsyncIterator[CanonicalStreamChunk]:

        ...

class ChatOrchestratorService(IChatOrchestrator):

    def __init__(
        self,
        llm_client: ILLMClient,
        knowledge_service: Optional[KnowledgeService] = None,
        retrieval_service: Optional[IRetrievalService | AuthorizedRetrievalService] = None,
        max_tool_iterations: Optional[int] = None,
        tool_timeout_seconds: Optional[float] = None,
        max_hidden_turn_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.llm_client = llm_client
        self.knowledge_service = knowledge_service
        self.retrieval_service = retrieval_service
        self.max_tool_iterations = (
            max_tool_iterations
            if max_tool_iterations is not None
            else getattr(get_settings().gateway, "max_tool_iterations", 10)
        )
        self.tool_timeout_seconds = (
            tool_timeout_seconds
            if tool_timeout_seconds is not None
            else getattr(get_settings().gateway, "tool_timeout_seconds", 15.0)
        )
        if self.tool_timeout_seconds <= 0 or max_hidden_turn_bytes <= 0:
            raise ValueError("Tool timeout and hidden turn buffer limit must be positive")
        self.max_hidden_turn_bytes = max_hidden_turn_bytes

    def _prepare_tools(
        self, client_tools: List[ToolDefinition]
    ) -> Tuple[List[ToolDefinition], Optional[List[Dict[str, Any]]]]:

        reserved = [
            tool.function.name
            for tool in client_tools
            if is_internal_tool(tool.function.name)
        ]
        if reserved:
            raise ValidationException("Client tools cannot use reserved gateway tool names.")
        combined_tools: List[ToolDefinition] = list(client_tools)
        existing_names = {t.function.name for t in client_tools}

        for internal_td in get_internal_tool_definitions():
            if internal_td.function.name not in existing_names:
                combined_tools.append(internal_td)
                existing_names.add(internal_td.function.name)

        if not combined_tools:
            return [], None

        upstream_tools: List[Dict[str, Any]] = []
        for td in combined_tools:
            upstream_tools.append({
                "type": "function",
                "function": {
                    "name": td.function.name,
                    "description": td.function.description,
                    "parameters": td.function.parameters,
                },
            })

        return combined_tools, upstream_tools

    def _prepare_upstream_messages(
        self, messages: List[CanonicalMessage], system_prompt: Optional[str] = None
    ) -> List[Dict[str, Any]]:

        upstream_msgs: List[Dict[str, Any]] = []

        settings = get_settings()
        prompt_enabled = getattr(settings.gateway, "knowledge_system_prompt_enabled", True)
        custom_prompt = getattr(settings.gateway, "knowledge_system_prompt_custom", None)

        system_msgs_content = [
            m.text_content for m in messages if m.role == "system" and m.text_content
        ]
        has_system_msg = len(system_msgs_content) > 0

        base_client_prompt = None
        if system_prompt and system_prompt.strip():
            base_client_prompt = system_prompt.strip()
        elif has_system_msg:
            base_client_prompt = "\n\n".join(c.strip() for c in system_msgs_content if c.strip())

        final_system_prompt = compose_system_prompt(
            client_system_prompt=base_client_prompt,
            enabled=prompt_enabled,
            custom_prompt=custom_prompt,
        )

        if final_system_prompt:
            upstream_msgs.append({"role": "system", "content": final_system_prompt})

        for msg in messages:
            if msg.role == "system":
                continue
            elif msg.role == "user":

                for tr in msg.tool_results:
                    content_str = (
                        tr.content
                        if isinstance(tr.content, str)
                        else json.dumps(tr.content, ensure_ascii=False)
                    )
                    upstream_msgs.append({
                        "role": "tool",
                        "tool_call_id": tr.tool_use_id,
                        "content": content_str,
                    })
                text = msg.text_content
                if text:
                    upstream_msgs.append({"role": "user", "content": text})
            elif msg.role == "assistant":
                msg_dict: Dict[str, Any] = {"role": "assistant"}
                text = msg.text_content
                if text:
                    msg_dict["content"] = text
                else:
                    msg_dict["content"] = None

                thinking = msg.thinking_content
                if thinking:
                    msg_dict["reasoning_content"] = thinking

                tool_uses = msg.tool_uses
                if tool_uses:
                    tc_list = []
                    for tu in tool_uses:
                        args_str = (
                            json.dumps(tu.input, ensure_ascii=False)
                            if isinstance(tu.input, (dict, list))
                            else str(tu.input)
                        )
                        tc_list.append({
                            "id": tu.id,
                            "type": "function",
                            "function": {
                                "name": tu.name,
                                "arguments": args_str,
                            },
                        })
                    msg_dict["tool_calls"] = tc_list
                upstream_msgs.append(msg_dict)
            elif msg.role == "tool":
                for tr in msg.tool_results:
                    content_str = (
                        tr.content
                        if isinstance(tr.content, str)
                        else json.dumps(tr.content, ensure_ascii=False)
                    )
                    upstream_msgs.append({
                        "role": "tool",
                        "tool_call_id": tr.tool_use_id,
                        "content": content_str,
                    })

        return upstream_msgs

    async def _execute_internal_tool(
        self,
        tool_call: ToolCall,
        workspace_id: str,
    ) -> CanonicalToolResultBlock:

        name = tool_call.function.name
        raw_args = tool_call.function.arguments
        call_id = tool_call.id

        try:
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except Exception:
                    return CanonicalToolResultBlock(
                        tool_use_id=call_id,
                        content=json.dumps(
                            {"error": "Invalid JSON arguments.", "type": "validation_error"}
                        ),
                        is_error=True,
                    )
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}

            if not is_internal_tool(name):
                return CanonicalToolResultBlock(
                    tool_use_id=call_id,
                    content=json.dumps(
                        {"error": f"'{name}' is not an internal tool", "type": "tool_execution_error"}
                    ),
                    is_error=True,
                )

            if name == "knowledge_search" and self.retrieval_service is not None:
                query = str(args.get("query", ""))
                if not query or not query.strip():
                    return CanonicalToolResultBlock(
                        tool_use_id=call_id,
                        content=json.dumps(
                            {"error": "Missing required 'query' parameter.", "type": "validation_error"}
                        ),
                        is_error=True,
                    )
                limit = int(args.get("limit", 5))
                if limit < 1 or limit > 20:
                    raise ValidationException("Search limit must be between 1 and 20.")
                try:
                    if isinstance(self.retrieval_service, AuthorizedRetrievalService):
                        principal = get_bound_principal()
                        if principal is None:
                            raise AuthorizationException()
                        requested_workspace = args.get("workspace_id")
                        response = await self.retrieval_service.search(
                            principal,
                            query,
                            requested_space_ids=(
                                {str(requested_workspace)}
                                if requested_workspace
                                else None
                            ),
                            active_space_id=workspace_id,
                            limit=limit,
                        )
                        content_payload = [
                            {
                                "id": str(hit.candidate.canonical_id),
                                "revision_id": str(hit.candidate.revision_id),
                                "citation": hit.candidate.citation_uri,
                                "title": hit.candidate.title,
                                "content": hit.candidate.content,
                                "score": hit.rank_score,
                                "source_type": hit.candidate.source_type,
                                "workspace_id": hit.candidate.space_id,
                                "version": hit.candidate.version,
                            }
                            for hit in response.hits
                        ]
                        health_payload = {
                            "semantic_status": response.health.semantic_status,
                            "degraded_reasons": list(response.health.degraded_reasons),
                            "embedding_coverage": response.health.embedding_coverage,
                            "abstained": response.explanation.abstained,
                        }
                    else:
                        ws_id = args.get("workspace_id") or workspace_id
                        results = await self.retrieval_service.hybrid_search(
                            query=query,
                            workspace_id=ws_id,
                            limit=limit,
                        )
                        content_payload = [
                            {
                                "id": r.id,
                                "title": r.title,
                                "content": r.content,
                                "score": r.normalized_score or r.rrf_score,
                                "source_type": r.source_type,
                                "workspace_id": r.workspace_id,
                                "version": r.version,
                            }
                            for r in results
                        ]
                        health_payload = None
                    return CanonicalToolResultBlock(
                        tool_use_id=call_id,
                        content=json.dumps(
                            {
                                "results": content_payload,
                                "count": len(content_payload),
                                "health": health_payload,
                            }
                        ),
                        is_error=False,
                    )
                except Exception as exc:
                    logger.warning(
                        "Hybrid retrieval tool failed",
                        extra={"exception_class": type(exc).__name__},
                    )
                    return CanonicalToolResultBlock(
                        tool_use_id=call_id,
                        content=json.dumps(
                            {
                                "error": "Knowledge retrieval is currently unavailable.",
                                "type": "retrieval_error",
                            }
                        ),
                        is_error=True,
                    )

            if self.knowledge_service is not None:
                tool_result: ToolResult = await self.knowledge_service.execute_tool(
                    tool_call_id=call_id,
                    name=name,
                    arguments=args,
                    session_workspace_id=workspace_id,
                )
                return CanonicalToolResultBlock(
                    tool_use_id=call_id,
                    content=tool_result.content,
                    is_error=tool_result.is_error,
                )

            return CanonicalToolResultBlock(
                tool_use_id=call_id,
                content=json.dumps({"status": "executed", "tool": name, "arguments": args}),
                is_error=False,
            )

        except (ConcurrencyConflictException, ItemNotFoundException, ValidationException) as exc:
            return CanonicalToolResultBlock(
                tool_use_id=call_id,
                content=json.dumps(exc.to_dict()),
                is_error=True,
            )
        except Exception as exc:
            logger.error(
                "Internal tool execution failed",
                extra={"tool_name": name, "exception_class": type(exc).__name__},
            )
            return CanonicalToolResultBlock(
                tool_use_id=call_id,
                content=json.dumps(
                    {
                        "error": "The tool could not complete the request.",
                        "type": "tool_execution_error",
                    }
                ),
                is_error=True,
            )

    async def _execute_internal_tool_bounded(
        self,
        tool_call: ToolCall,
        workspace_id: str,
    ) -> CanonicalToolResultBlock:
        try:
            return await asyncio.wait_for(
                self._execute_internal_tool(tool_call, workspace_id),
                timeout=self.tool_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ToolExecutionException("Internal tool execution timed out.") from exc

    async def orchestrate_chat(
        self,
        request: CanonicalChatRequest,
        workspace_id: str = get_settings().gateway.default_workspace_id,
    ) -> CanonicalChatResponse:

        default_ws = get_settings().gateway.default_workspace_id
        effective_workspace = workspace_id if workspace_id != default_ws else (request.workspace_id or default_ws)
        combined_tool_defs, upstream_tools = self._prepare_tools(request.tools)

        conversation_messages: List[CanonicalMessage] = list(request.messages)
        iteration = 0
        total_usage = CanonicalUsage()

        while iteration < self.max_tool_iterations:
            iteration += 1

            upstream_messages = self._prepare_upstream_messages(
                conversation_messages,
                system_prompt=request.system_prompt,
            )

            extra_kwargs = getattr(request, "extra_params", {}) or {}
            llm_response: CanonicalLLMResponse = await self.llm_client.generate(
                messages=upstream_messages,
                tools=upstream_tools,
                model=request.model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                stop=request.stop if request.stop else None,
                tool_choice=request.tool_choice,
                **extra_kwargs,
            )

            if llm_response.usage:
                total_usage.prompt_tokens += llm_response.usage.prompt_tokens
                total_usage.completion_tokens += llm_response.usage.completion_tokens
                total_usage.total_tokens += llm_response.usage.total_tokens

            if not llm_response.tool_calls:
                blocks: List[CanonicalBlock] = []
                if llm_response.reasoning_content:
                    blocks.append(CanonicalThinkingBlock(thinking=llm_response.reasoning_content))
                if llm_response.content:
                    blocks.append(CanonicalTextBlock(text=llm_response.content))

                finish_reason = llm_response.finish_reason or "stop"
                if finish_reason not in ("stop", "tool_use", "max_tokens", "content_filter", "error"):
                    finish_reason = "stop"

                return CanonicalChatResponse(
                    id=llm_response.id,
                    model=llm_response.model,
                    role="assistant",
                    content=blocks,
                    finish_reason=finish_reason,  # type: ignore[arg-type]
                    usage=total_usage,
                )

            if len(llm_response.tool_calls) > 64:
                raise ToolExecutionException("Upstream returned too many tool calls.")
            has_internal_tool = any(
                is_internal_tool(tc.function.name) for tc in llm_response.tool_calls
            )
            has_external_tool = any(
                not is_internal_tool(tc.function.name) for tc in llm_response.tool_calls
            )
            if has_internal_tool and has_external_tool:
                raise ToolExecutionException(
                    "Mixed internal and external tool calls are not supported."
                )
            if has_external_tool:
                blocks = []
                if llm_response.reasoning_content:
                    blocks.append(CanonicalThinkingBlock(thinking=llm_response.reasoning_content))
                if llm_response.content:
                    blocks.append(CanonicalTextBlock(text=llm_response.content))

                for tc in llm_response.tool_calls:
                    try:
                        args = (
                            json.loads(tc.function.arguments)
                            if isinstance(tc.function.arguments, str)
                            else (tc.function.arguments or {})
                        )
                    except Exception:
                        args = {"raw_arguments": tc.function.arguments}

                    blocks.append(
                        CanonicalToolUseBlock(
                            id=tc.id,
                            name=tc.function.name,
                            input=args,
                        )
                    )

                return CanonicalChatResponse(
                    id=llm_response.id,
                    model=llm_response.model,
                    role="assistant",
                    content=blocks,
                    finish_reason="tool_use",
                    usage=total_usage,
                )

            assistant_blocks: List[CanonicalBlock] = []
            if llm_response.reasoning_content:
                assistant_blocks.append(
                    CanonicalThinkingBlock(thinking=llm_response.reasoning_content)
                )
            if llm_response.content:
                assistant_blocks.append(CanonicalTextBlock(text=llm_response.content))

            for tc in llm_response.tool_calls:
                try:
                    args = (
                        json.loads(tc.function.arguments)
                        if isinstance(tc.function.arguments, str)
                        else (tc.function.arguments or {})
                    )
                except Exception:
                    args = {"raw_arguments": tc.function.arguments}

                assistant_blocks.append(
                    CanonicalToolUseBlock(
                        id=tc.id,
                        name=tc.function.name,
                        input=args,
                    )
                )

            conversation_messages.append(
                CanonicalMessage(
                    role="assistant",
                    content=assistant_blocks,
                )
            )

            tool_result_blocks: List[CanonicalToolResultBlock] = []
            for tc in llm_response.tool_calls:
                result_block = await self._execute_internal_tool_bounded(
                    tc,
                    effective_workspace,
                )
                tool_result_blocks.append(result_block)

            conversation_messages.append(
                CanonicalMessage(
                    role="tool",
                    content=tool_result_blocks,
                    tool_call_id=(
                        llm_response.tool_calls[0].id if len(llm_response.tool_calls) == 1 else None
                    ),
                )
            )

        raise ToolExecutionException("Tool execution budget exhausted.")

    async def orchestrate_chat_stream(
        self,
        request: CanonicalChatRequest,
        workspace_id: str = get_settings().gateway.default_workspace_id,
    ) -> AsyncIterator[CanonicalStreamChunk]:

        default_ws = get_settings().gateway.default_workspace_id
        effective_workspace = workspace_id if workspace_id != default_ws else (request.workspace_id or default_ws)
        combined_tool_defs, upstream_tools = self._prepare_tools(request.tools)

        conversation_messages: List[CanonicalMessage] = list(request.messages)
        iteration = 0
        while iteration < self.max_tool_iterations:
            iteration += 1

            upstream_messages = self._prepare_upstream_messages(
                conversation_messages,
                system_prompt=request.system_prompt,
            )

            extra_kwargs = getattr(request, "extra_params", {}) or {}
            stream_iter = self.llm_client.generate_stream(
                messages=upstream_messages,
                tools=upstream_tools,
                model=request.model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                top_p=request.top_p,
                stop=request.stop if request.stop else None,
                tool_choice=request.tool_choice,
                **extra_kwargs,
            )

            turn_chunks: List[CanonicalLLMStreamChunk] = []
            turn_text_fragments: List[str] = []
            turn_thinking_fragments: List[str] = []
            turn_tool_calls_dict: Dict[int, Dict[str, Any]] = {}
            last_chunk_id = f"stream-{uuid.uuid4().hex[:8]}"
            last_model = request.model
            buffered_bytes = 0

            async for chunk in stream_iter:
                turn_chunks.append(chunk)
                buffered_bytes += len((chunk.delta_content or "").encode("utf-8"))
                buffered_bytes += len((chunk.delta_reasoning_content or "").encode("utf-8"))
                if chunk.delta_tool_calls:
                    buffered_bytes += sum(
                        len((tool.function.name or "").encode("utf-8"))
                        + len((tool.function.arguments or "").encode("utf-8"))
                        for tool in chunk.delta_tool_calls
                    )
                if buffered_bytes > self.max_hidden_turn_bytes:
                    raise ToolExecutionException("Upstream turn exceeded the safe buffering budget.")
                if chunk.id:
                    last_chunk_id = chunk.id
                if chunk.model:
                    last_model = chunk.model

                if chunk.delta_content is not None:
                    turn_text_fragments.append(chunk.delta_content)

                if chunk.delta_reasoning_content is not None:
                    turn_thinking_fragments.append(chunk.delta_reasoning_content)

                if chunk.delta_tool_calls:
                    for idx, tc in enumerate(chunk.delta_tool_calls):

                        slot = tc.index if tc.index is not None else idx
                        if slot not in turn_tool_calls_dict:
                            if len(turn_tool_calls_dict) >= 64:
                                raise ToolExecutionException("Upstream returned too many tool calls.")
                            turn_tool_calls_dict[slot] = {
                                "id": tc.id or f"call_{uuid.uuid4().hex[:6]}",
                                "name": tc.function.name or "",
                                "arguments": tc.function.arguments or "",
                            }
                        else:
                            if tc.id:
                                turn_tool_calls_dict[slot]["id"] = tc.id
                            if tc.function.name:
                                turn_tool_calls_dict[slot]["name"] = tc.function.name
                            if tc.function.arguments:
                                turn_tool_calls_dict[slot]["arguments"] += tc.function.arguments

            reconstructed_tool_calls: List[ToolCall] = []
            for idx in sorted(turn_tool_calls_dict.keys()):
                tc_data = turn_tool_calls_dict[idx]
                reconstructed_tool_calls.append(
                    ToolCall(
                        id=tc_data["id"],
                        function=FunctionCall(
                            name=tc_data["name"],
                            arguments=tc_data["arguments"],
                        ),
                    )
                )

            has_internal_tool = any(
                is_internal_tool(tool.function.name)
                for tool in reconstructed_tool_calls
            )
            has_external_tool = any(
                not is_internal_tool(tool.function.name)
                for tool in reconstructed_tool_calls
            )
            if has_internal_tool and has_external_tool:
                raise ToolExecutionException(
                    "Mixed internal and external tool calls are not supported."
                )

            if not has_internal_tool:
                for chunk in turn_chunks:
                    finish_reason = chunk.finish_reason
                    if finish_reason in ("tool_calls", "tool_use"):
                        finish_reason = "tool_use"
                    elif finish_reason and finish_reason not in (
                        "stop",
                        "tool_use",
                        "max_tokens",
                        "content_filter",
                        "error",
                    ):
                        finish_reason = "stop"
                    yield CanonicalStreamChunk(
                        id=chunk.id or last_chunk_id,
                        model=chunk.model or last_model,
                        delta_content=chunk.delta_content,
                        delta_thinking=chunk.delta_reasoning_content,
                        delta_tool_calls=chunk.delta_tool_calls,
                        finish_reason=finish_reason,  # type: ignore[arg-type]
                        usage=chunk.usage,
                    )
                return

            if has_internal_tool:

                full_text = "".join(turn_text_fragments)
                full_thinking = "".join(turn_thinking_fragments)
                assistant_blocks: List[CanonicalBlock] = []
                if full_thinking:
                    assistant_blocks.append(CanonicalThinkingBlock(thinking=full_thinking))
                if full_text:
                    assistant_blocks.append(CanonicalTextBlock(text=full_text))

                for tc in reconstructed_tool_calls:
                    try:
                        args = (
                            json.loads(tc.function.arguments)
                            if tc.function.arguments
                            else {}
                        )
                    except Exception:
                        args = {"raw_arguments": tc.function.arguments}

                    assistant_blocks.append(
                        CanonicalToolUseBlock(
                            id=tc.id,
                            name=tc.function.name,
                            input=args,
                        )
                    )

                conversation_messages.append(
                    CanonicalMessage(
                        role="assistant",
                        content=assistant_blocks,
                    )
                )

                tool_result_blocks: List[CanonicalToolResultBlock] = []
                for tc in reconstructed_tool_calls:
                    result_block = await self._execute_internal_tool_bounded(
                        tc,
                        effective_workspace,
                    )
                    tool_result_blocks.append(result_block)

                conversation_messages.append(
                    CanonicalMessage(
                        role="tool",
                        content=tool_result_blocks,
                        tool_call_id=(
                            reconstructed_tool_calls[0].id
                            if len(reconstructed_tool_calls) == 1
                            else None
                        ),
                    )
                )
                continue

        raise ToolExecutionException("Tool execution budget exhausted.")
