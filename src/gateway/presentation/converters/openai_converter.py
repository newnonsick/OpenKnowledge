

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Union

from src.gateway.config import get_settings, settings
from src.gateway.domain.canonical import (
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalThinkingBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
)
from src.gateway.domain.tools import (
    FunctionCall,
    FunctionDefinition,
    ToolCall,
    ToolDefinition,
)
from src.gateway.presentation.schemas.openai_schemas import (
    OpenAIChatChoice,
    OpenAIChatChoiceMessage,
    OpenAIChatCompletionChunk,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
    OpenAIChatMessage,
    OpenAIChatStreamChoice,
    OpenAIChatStreamDelta,
    OpenAIFunctionCall,
    OpenAIToolCall,
    OpenAIStreamChoice,
    OpenAIStreamDelta,
    OpenAIUsage,
)

_CANONICAL_TO_OPENAI_FINISH = {
    "stop": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "content_filter": "content_filter",
}

def map_finish_reason_to_openai(finish_reason: Optional[str]) -> Optional[str]:

    if not finish_reason:
        return None
    return _CANONICAL_TO_OPENAI_FINISH.get(finish_reason, "stop")

def openai_request_to_canonical(
    req: OpenAIChatCompletionRequest,
    workspace_id: Optional[str] = None,
) -> CanonicalChatRequest:

    canonical_messages: List[CanonicalMessage] = []
    system_prompts: List[str] = []

    for msg in req.messages:
        role = msg.role
        blocks: List[CanonicalBlock] = []

        if role in ("system", "developer"):

            text_val = msg.content if isinstance(msg.content, str) else ""
            if text_val:
                system_prompts.append(text_val)
                blocks.append(CanonicalTextBlock(text=text_val))
            canonical_messages.append(CanonicalMessage(role="system", content=blocks))

        elif role == "user":
            if isinstance(msg.content, str):
                blocks.append(CanonicalTextBlock(text=msg.content))
            elif isinstance(msg.content, list):
                for part in msg.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        blocks.append(CanonicalTextBlock(text=part.get("text", "")))
            canonical_messages.append(CanonicalMessage(role="user", content=blocks))

        elif role == "assistant":
            if msg.reasoning_content:
                blocks.append(CanonicalThinkingBlock(thinking=msg.reasoning_content))
            if isinstance(msg.content, str) and msg.content:
                blocks.append(CanonicalTextBlock(text=msg.content))
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    arguments_dict: Dict[str, Any] = {}
                    if tc.function.arguments:
                        try:
                            arguments_dict = json.loads(tc.function.arguments)
                        except Exception:
                            arguments_dict = {"raw_arguments": tc.function.arguments}
                    blocks.append(
                        CanonicalToolUseBlock(
                            id=tc.id,
                            name=tc.function.name,
                            input=arguments_dict,
                        )
                    )
            canonical_messages.append(CanonicalMessage(role="assistant", content=blocks))

        elif role in ("tool", "function"):
            tool_id = msg.tool_call_id or msg.name or ""
            content_val = msg.content if isinstance(msg.content, str) else json.dumps(msg.content or {})
            blocks.append(
                CanonicalToolResultBlock(
                    tool_use_id=tool_id,
                    content=content_val,
                    is_error=False,
                )
            )
            canonical_messages.append(
                CanonicalMessage(
                    role="tool",
                    content=blocks,
                    tool_call_id=tool_id,
                    name=msg.name,
                )
            )

    canonical_tools: List[ToolDefinition] = []
    if req.tools:
        for t in req.tools:
            fn = t.function
            canonical_tools.append(
                ToolDefinition(
                    type="function",
                    function=FunctionDefinition(
                        name=fn.name,
                        description=fn.description or "",
                        parameters=fn.parameters or {},
                    ),
                )
            )

    stop_list: List[str] = []
    if isinstance(req.stop, str):
        stop_list = [req.stop]
    elif isinstance(req.stop, list):
        stop_list = req.stop

    default_ws = get_settings().gateway.default_workspace_id
    active_workspace = workspace_id or req.workspace_id or default_ws
    combined_system_prompt = "\n\n".join(system_prompts) if system_prompts else None

    extra_params: Dict[str, Any] = {}
    if req.presence_penalty is not None:
        extra_params["presence_penalty"] = req.presence_penalty
    if req.frequency_penalty is not None:
        extra_params["frequency_penalty"] = req.frequency_penalty
    if req.user is not None:
        extra_params["user"] = req.user
    if req.n is not None and req.n != 1:
        extra_params["n"] = req.n

    return CanonicalChatRequest(
        model=req.model,
        messages=canonical_messages,
        system_prompt=combined_system_prompt,
        temperature=req.temperature,
        top_p=req.top_p,
        max_tokens=req.max_tokens,
        stop=stop_list,
        stream=bool(req.stream),
        tools=canonical_tools,
        tool_choice=req.tool_choice,
        workspace_id=active_workspace,
        extra_params=extra_params,
    )

def canonical_response_to_openai(
    resp: CanonicalChatResponse,
) -> OpenAIChatCompletionResponse:

    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[OpenAIToolCall] = []

    for block in resp.content:
        if isinstance(block, CanonicalThinkingBlock):
            reasoning_parts.append(block.thinking)
        elif isinstance(block, CanonicalTextBlock):
            text_parts.append(block.text)
        elif isinstance(block, CanonicalToolUseBlock):
            args_str = json.dumps(block.input, ensure_ascii=False)
            tool_calls.append(
                OpenAIToolCall(
                    id=block.id,
                    type="function",
                    function=OpenAIFunctionCall(
                        name=block.name,
                        arguments=args_str,
                    ),
                )
            )

    content_str = "".join(text_parts) if text_parts else None

    finish_reason = map_finish_reason_to_openai(resp.finish_reason) or "stop"
    if tool_calls:
        finish_reason = "tool_calls"

    choice_message = OpenAIChatChoiceMessage(
        role="assistant",
        content=content_str,
        reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
        tool_calls=tool_calls if tool_calls else None,
    )

    choice = OpenAIChatChoice(
        index=0,
        message=choice_message,
        finish_reason=finish_reason,
    )

    usage = OpenAIUsage(
        prompt_tokens=resp.usage.prompt_tokens,
        completion_tokens=resp.usage.completion_tokens,
        total_tokens=resp.usage.total_tokens,
    )

    return OpenAIChatCompletionResponse(
        id=resp.id,
        object="chat.completion",
        created=int(time.time()),
        model=resp.model,
        choices=[choice],
        usage=usage,
    )

def canonical_stream_chunk_to_openai(
    chunk: CanonicalStreamChunk,
    created_ts: Optional[int] = None,
) -> OpenAIChatCompletionChunk:

    tool_calls_payload: Optional[List[Dict[str, Any]]] = None
    if chunk.delta_tool_calls:
        tool_calls_payload = []
        for idx, tc in enumerate(chunk.delta_tool_calls):
            tool_calls_payload.append({
                "index": tc.index if tc.index is not None else idx,
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            })

    finish_reason: Optional[str] = map_finish_reason_to_openai(chunk.finish_reason)

    delta = OpenAIStreamDelta(
        role=None,
        content=chunk.delta_content,
        reasoning_content=chunk.delta_thinking,
        tool_calls=tool_calls_payload,
    )

    choice = OpenAIStreamChoice(
        index=0,
        delta=delta,
        finish_reason=finish_reason,
    )

    usage_payload: Optional[OpenAIUsage] = None
    if chunk.usage:
        usage_payload = OpenAIUsage(
            prompt_tokens=chunk.usage.prompt_tokens,
            completion_tokens=chunk.usage.completion_tokens,
            total_tokens=chunk.usage.total_tokens,
        )

    return OpenAIChatCompletionChunk(
        id=chunk.id,
        object="chat.completion.chunk",
        created=created_ts or int(time.time()),
        model=chunk.model,
        choices=[choice],
        usage=usage_payload,
    )
