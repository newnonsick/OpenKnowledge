from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


class ResponsesInputMessage(BaseModel):

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "developer", "user", "assistant"] = "user"
    content: Union[str, List[Dict[str, Any]]]


class ResponsesRequest(BaseModel):

    model_config = ConfigDict(extra="forbid")

    model: str
    input: Union[str, List[ResponsesInputMessage]]
    stream: Optional[bool] = False
    max_output_tokens: Optional[int] = Field(default=None, gt=0)
    metadata: Optional[Dict[str, str]] = None
    workspace_id: Optional[str] = None


class ResponsesOutputText(BaseModel):

    type: Literal["output_text"] = "output_text"
    text: str


class ResponsesOutputMessage(BaseModel):

    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: List[ResponsesOutputText] = Field(default_factory=list)


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
    output: List[ResponsesOutputMessage] = Field(default_factory=list)
    usage: ResponsesUsage = Field(default_factory=ResponsesUsage)
