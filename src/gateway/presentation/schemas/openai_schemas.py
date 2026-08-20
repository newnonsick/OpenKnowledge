

from __future__ import annotations

import time
from src.gateway.config import get_settings, settings
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator

class OpenAIFunctionDefinition(BaseModel):

    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = Field(default_factory=dict)

class OpenAIToolDefinition(BaseModel):

    model_config = ConfigDict(extra="forbid")

    type: Literal["function"] = "function"
    function: OpenAIFunctionDefinition

class OpenAIFunctionCall(BaseModel):

    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: str = "{}"

class OpenAIToolCall(BaseModel):

    model_config = ConfigDict(extra="forbid")

    id: str
    type: Literal["function"] = "function"
    function: OpenAIFunctionCall
    index: Optional[int] = None

class OpenAIChatMessage(BaseModel):

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: Optional[Union[str, List[Dict[str, Any]], Dict[str, Any]]] = None
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[OpenAIToolCall]] = None
    reasoning_content: Optional[str] = None

    @field_validator("content", mode="before")
    @classmethod
    def validate_content(cls, v: Any) -> Any:
        if v is None:
            return ""
        if isinstance(v, list):
            for part in v:
                if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                    raise ValueError("Only text content blocks are supported")
        elif not isinstance(v, str):
            raise ValueError("Only text message content is supported")
        return v

class OpenAIChatCompletionRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")

    model: str
    messages: List[OpenAIChatMessage] = Field(default_factory=list)
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    n: Optional[int] = Field(default=1, ge=1)
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    max_tokens: Optional[int] = Field(default=None, gt=0)
    presence_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    user: Optional[str] = None
    tools: Optional[List[OpenAIToolDefinition]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    workspace_id: Optional[str] = Field(default_factory=lambda: get_settings().gateway.default_workspace_id)

class OpenAIUsage(BaseModel):

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class OpenAIChatChoiceMessage(BaseModel):

    role: Literal["assistant"] = "assistant"
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: Optional[List[OpenAIToolCall]] = None

class OpenAIChatChoice(BaseModel):

    index: int = 0
    message: OpenAIChatChoiceMessage
    finish_reason: Optional[str] = "stop"

class OpenAIChatCompletionResponse(BaseModel):

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[OpenAIChatChoice] = Field(default_factory=list)
    usage: OpenAIUsage = Field(default_factory=OpenAIUsage)
    system_fingerprint: Optional[str] = "fp_gateway_m2"

class OpenAIStreamDelta(BaseModel):

    role: Optional[str] = None
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None

OpenAIChatStreamDelta = OpenAIStreamDelta

class OpenAIStreamChoice(BaseModel):

    index: int = 0
    delta: OpenAIStreamDelta
    finish_reason: Optional[str] = None

OpenAIChatStreamChoice = OpenAIStreamChoice

class OpenAIChatCompletionChunk(BaseModel):

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[OpenAIStreamChoice] = Field(default_factory=list)
    usage: Optional[OpenAIUsage] = None
    system_fingerprint: Optional[str] = "fp_gateway_m2"

OpenAIStreamChunk = OpenAIChatCompletionChunk

class OpenAIModelObject(BaseModel):

    id: str
    object: Literal["model"] = "model"
    created: int = 1700000000
    owned_by: str = "gateway"
    root: Optional[str] = None

class OpenAIModelListResponse(BaseModel):

    object: Literal["list"] = "list"
    data: List[OpenAIModelObject] = Field(default_factory=list)

class OpenAIErrorDetail(BaseModel):

    message: str
    type: str = "invalid_request_error"
    param: Optional[str] = None
    code: Optional[Union[str, int]] = None

class OpenAIErrorResponse(BaseModel):

    error: OpenAIErrorDetail
