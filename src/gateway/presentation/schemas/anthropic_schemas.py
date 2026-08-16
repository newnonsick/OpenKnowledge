

from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator

class AnthropicTextBlock(BaseModel):

    type: Literal["text"] = "text"
    text: str

class AnthropicImageSource(BaseModel):

    type: Literal["base64"] = "base64"
    media_type: str
    data: str

class AnthropicImageBlock(BaseModel):

    type: Literal["image"] = "image"
    source: AnthropicImageSource

class AnthropicToolUseBlock(BaseModel):

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: Dict[str, Any] = Field(default_factory=dict)

class AnthropicToolResultBlock(BaseModel):

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]]]
    is_error: Optional[bool] = False

AnthropicContentBlock = Union[
    AnthropicTextBlock,
    AnthropicImageBlock,
    AnthropicToolUseBlock,
    AnthropicToolResultBlock,
    Dict[str, Any],
]

class AnthropicMessageParam(BaseModel):

    role: Literal["user", "assistant"]
    content: Union[str, List[AnthropicContentBlock]]

    @field_validator("content", mode="before")
    @classmethod
    def validate_content(cls, v: Any) -> Any:
        if v is None:
            return ""
        return v

class AnthropicToolParam(BaseModel):

    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any] = Field(default_factory=dict)

class AnthropicMessagesRequest(BaseModel):

    model: str
    messages: List[AnthropicMessageParam] = Field(default_factory=list)
    system: Optional[Union[str, List[AnthropicTextBlock]]] = None
    max_tokens: Optional[int] = Field(default=1024, gt=0)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    top_k: Optional[int] = Field(default=None, ge=0)
    stream: Optional[bool] = False
    stop_sequences: Optional[List[str]] = None
    tools: Optional[List[AnthropicToolParam]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None

class AnthropicUsage(BaseModel):

    input_tokens: int = 0
    output_tokens: int = 0

class AnthropicMessagesResponse(BaseModel):

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: List[AnthropicContentBlock] = Field(default_factory=list)
    model: str
    stop_reason: Optional[Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]] = "end_turn"
    stop_sequence: Optional[str] = None
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage)

class AnthropicMessageStartEvent(BaseModel):

    type: Literal["message_start"] = "message_start"
    message: AnthropicMessagesResponse

class AnthropicContentBlockStartEvent(BaseModel):

    type: Literal["content_block_start"] = "content_block_start"
    index: int
    content_block: AnthropicContentBlock

class AnthropicTextDelta(BaseModel):

    type: Literal["text_delta"] = "text_delta"
    text: str

class AnthropicInputJsonDelta(BaseModel):

    type: Literal["input_json_delta"] = "input_json_delta"
    partial_json: str

class AnthropicContentBlockDeltaEvent(BaseModel):

    type: Literal["content_block_delta"] = "content_block_delta"
    index: int
    delta: Union[AnthropicTextDelta, AnthropicInputJsonDelta, Dict[str, Any]]

class AnthropicContentBlockStopEvent(BaseModel):

    type: Literal["content_block_stop"] = "content_block_stop"
    index: int

class AnthropicMessageDeltaBody(BaseModel):

    stop_reason: Optional[Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]] = "end_turn"
    stop_sequence: Optional[str] = None

class AnthropicMessageDeltaUsage(BaseModel):

    output_tokens: int = 0

class AnthropicMessageDeltaEvent(BaseModel):

    type: Literal["message_delta"] = "message_delta"
    delta: AnthropicMessageDeltaBody = Field(default_factory=AnthropicMessageDeltaBody)
    usage: AnthropicMessageDeltaUsage = Field(default_factory=AnthropicMessageDeltaUsage)

class AnthropicMessageStopEvent(BaseModel):

    type: Literal["message_stop"] = "message_stop"

class AnthropicPingEvent(BaseModel):

    type: Literal["ping"] = "ping"

class AnthropicErrorDetail(BaseModel):

    type: str = "invalid_request_error"
    message: str

class AnthropicErrorResponse(BaseModel):

    type: Literal["error"] = "error"
    error: AnthropicErrorDetail
