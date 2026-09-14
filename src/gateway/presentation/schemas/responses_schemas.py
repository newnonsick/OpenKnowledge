from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


class ResponsesInputMessage(BaseModel):

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "developer", "user", "assistant"] = "user"
    content: Union[str, List[Dict[str, Any]]]


class ResponsesFunctionDefinition(BaseModel):

    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = Field(default_factory=dict)


class ResponsesTool(BaseModel):

    model_config = ConfigDict(extra="forbid")

    type: Literal["function"] = "function"
    function: ResponsesFunctionDefinition


class ResponsesReasoning(BaseModel):

    model_config = ConfigDict(extra="forbid")

    effort: Optional[Literal["low", "medium", "high"]] = None
    summary: Optional[str] = None


class ResponsesRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")

    model: str
    input: Union[str, List[ResponsesInputMessage]]
    stream: Optional[bool] = False
    max_output_tokens: Optional[int] = Field(default=None, gt=0)
    metadata: Optional[Dict[str, str]] = None
    workspace_id: Optional[str] = None
    instructions: Optional[str] = None
    tools: Optional[List[ResponsesTool]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    reasoning: Optional[ResponsesReasoning] = None
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    truncation: Optional[str] = None
    previous_response_id: Optional[str] = None
    parallel_tool_calls: Optional[bool] = None


class ResponsesOutputText(BaseModel):

    type: Literal["output_text"] = "output_text"
    text: str


class ResponsesOutputMessage(BaseModel):

    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: List[ResponsesOutputText] = Field(default_factory=list)


class ResponsesReasoningItem(BaseModel):

    type: Literal["reasoning"] = "reasoning"
    content: List[ResponsesOutputText] = Field(default_factory=list)


class ResponsesFunctionCallItem(BaseModel):

    type: Literal["function_call"] = "function_call"
    call_id: str
    name: str
    arguments: str = "{}"


class ResponsesUsage(BaseModel):

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ResponsesResponse(BaseModel):

    id: str
    object: Literal["response"] = "response"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    status: Literal["completed"] = "completed"
    output: List[Union[ResponsesOutputMessage, ResponsesReasoningItem, ResponsesFunctionCallItem]] = Field(default_factory=list)
    usage: ResponsesUsage = Field(default_factory=ResponsesUsage)
