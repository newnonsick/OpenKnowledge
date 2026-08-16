

from __future__ import annotations

import json
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
    FunctionDefinition,
    ToolDefinition,
)
from src.gateway.presentation.schemas.anthropic_schemas import (
    AnthropicContentBlock,
    AnthropicImageBlock,
    AnthropicMessageParam,
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    AnthropicTextBlock,
    AnthropicToolParam,
    AnthropicToolResultBlock,
    AnthropicToolUseBlock,
    AnthropicUsage,
)

def anthropic_request_to_canonical(
    req: AnthropicMessagesRequest,
    workspace_id: Optional[str] = None,
) -> CanonicalChatRequest:

    canonical_messages: List[CanonicalMessage] = []

    system_text: Optional[str] = None
    if isinstance(req.system, str):
        system_text = req.system
    elif isinstance(req.system, list):
        system_text = "\n\n".join(
            block.text for block in req.system if isinstance(block, AnthropicTextBlock)
        )

    if system_text:
        canonical_messages.append(
            CanonicalMessage(
                role="system",
                content=[CanonicalTextBlock(text=system_text)],
            )
        )

    for msg in req.messages:
        role = msg.role
        blocks: List[CanonicalBlock] = []

        if isinstance(msg.content, str):
            if msg.content:
                blocks.append(CanonicalTextBlock(text=msg.content))
        elif isinstance(msg.content, list):
            for item in msg.content:
                if isinstance(item, AnthropicTextBlock):
                    blocks.append(CanonicalTextBlock(text=item.text))
                elif isinstance(item, AnthropicToolUseBlock):
                    blocks.append(
                        CanonicalToolUseBlock(
                            id=item.id,
                            name=item.name,
                            input=item.input,
                        )
                    )
                elif isinstance(item, AnthropicToolResultBlock):
                    blocks.append(
                        CanonicalToolResultBlock(
                            tool_use_id=item.tool_use_id,
                            content=item.content,
                            is_error=bool(item.is_error),
                        )
                    )
                elif isinstance(item, dict):
                    block_type = item.get("type")
                    if block_type == "thinking":
                        blocks.append(
                            CanonicalThinkingBlock(
                                thinking=item.get("thinking", ""),
                                signature=item.get("signature"),
                            )
                        )
                    elif block_type == "text":
                        blocks.append(CanonicalTextBlock(text=item.get("text", "")))
                    elif block_type == "tool_use":
                        blocks.append(
                            CanonicalToolUseBlock(
                                id=item.get("id", ""),
                                name=item.get("name", ""),
                                input=item.get("input", {}),
                            )
                        )
                    elif block_type == "tool_result":
                        blocks.append(
                            CanonicalToolResultBlock(
                                tool_use_id=item.get("tool_use_id", ""),
                                content=item.get("content", ""),
                                is_error=bool(item.get("is_error", False)),
                            )
                        )

        canonical_messages.append(
            CanonicalMessage(
                role="user" if role == "user" else "assistant",
                content=blocks,
            )
        )

    canonical_tools: List[ToolDefinition] = []
    if req.tools:
        for t in req.tools:
            if isinstance(t, AnthropicToolParam):
                name = t.name
                desc = t.description or ""
                schema = t.input_schema
            elif isinstance(t, dict):
                name = t.get("name", "")
                desc = t.get("description", "")
                schema = t.get("input_schema", {})
            else:
                continue

            canonical_tools.append(
                ToolDefinition(
                    type="function",
                    function=FunctionDefinition(
                        name=name,
                        description=desc,
                        parameters=schema,
                    ),
                )
            )

    default_ws = get_settings().gateway.default_workspace_id
    active_workspace = workspace_id or default_ws

    extra_params: Dict[str, Any] = {}
    if req.top_k is not None:
        extra_params["top_k"] = req.top_k
    if req.metadata:
        extra_params["metadata"] = req.metadata

    return CanonicalChatRequest(
        model=req.model,
        messages=canonical_messages,
        system_prompt=system_text,
        temperature=req.temperature,
        top_p=req.top_p,
        max_tokens=req.max_tokens,
        stop=req.stop_sequences or [],
        stream=bool(req.stream),
        tools=canonical_tools,
        tool_choice=_map_tool_choice_to_openai(req.tool_choice),
        workspace_id=active_workspace,
        extra_params=extra_params,
    )

def _map_tool_choice_to_openai(tool_choice: Optional[Union[str, Dict[str, Any]]]) -> Optional[Union[str, Dict[str, Any]]]:

    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        choice_type = tool_choice
        name = None
    elif isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type", "")
        name = tool_choice.get("name")
    else:
        return None

    if choice_type == "tool" and name:
        return {"type": "function", "function": {"name": name}}
    if choice_type == "any":
        return "required"
    if choice_type == "auto":
        return "auto"
    if choice_type in ("none", "required"):
        return choice_type
    return None

def canonical_response_to_anthropic(
    resp: CanonicalChatResponse,
) -> AnthropicMessagesResponse:

    anthropic_content: List[AnthropicContentBlock] = []

    has_tool_use = False
    for block in resp.content:
        if isinstance(block, CanonicalThinkingBlock):
            if block.thinking:
                anthropic_content.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                    "signature": block.signature or "gateway-thinking-signature",
                })
        elif isinstance(block, CanonicalTextBlock):
            if block.text:
                anthropic_content.append(AnthropicTextBlock(type="text", text=block.text))
        elif isinstance(block, CanonicalToolUseBlock):
            has_tool_use = True
            anthropic_content.append(
                AnthropicToolUseBlock(
                    type="tool_use",
                    id=block.id,
                    name=block.name,
                    input=block.input,
                )
            )
        elif isinstance(block, CanonicalToolResultBlock):
            anthropic_content.append(
                AnthropicToolResultBlock(
                    type="tool_result",
                    tool_use_id=block.tool_use_id,
                    content=block.content,
                    is_error=block.is_error,
                )
            )

    if not anthropic_content:
        anthropic_content.append(AnthropicTextBlock(type="text", text=""))

    stop_reason: Optional[str] = "end_turn"
    if resp.finish_reason == "tool_use" or has_tool_use:
        stop_reason = "tool_use"
    elif resp.finish_reason == "max_tokens":
        stop_reason = "max_tokens"
    elif resp.finish_reason == "stop":
        stop_reason = "end_turn"
    else:
        stop_reason = resp.finish_reason or "end_turn"

    usage = AnthropicUsage(
        input_tokens=resp.usage.prompt_tokens,
        output_tokens=resp.usage.completion_tokens,
    )

    return AnthropicMessagesResponse(
        id=resp.id,
        type="message",
        role="assistant",
        content=anthropic_content,
        model=resp.model,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        stop_sequence=None,
        usage=usage,
    )
