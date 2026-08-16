

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator

from src.gateway.config import get_settings, settings
from .tools import ToolCall, ToolDefinition

class CanonicalThinkingBlock(BaseModel):

    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: Optional[str] = None

class CanonicalTextBlock(BaseModel):

    type: Literal["text"] = "text"
    text: str

class CanonicalToolUseBlock(BaseModel):

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: Dict[str, Any]

class CanonicalToolResultBlock(BaseModel):

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]]]
    is_error: bool = False

CanonicalBlock = Union[
    CanonicalThinkingBlock,
    CanonicalTextBlock,
    CanonicalToolUseBlock,
    CanonicalToolResultBlock,
]

class CanonicalMessage(BaseModel):

    role: Literal["system", "user", "assistant", "tool"]
    content: List[CanonicalBlock] = Field(default_factory=list)
    name: Optional[str] = None
    tool_call_id: Optional[str] = None

    @field_validator("content", mode="before")
    @classmethod
    def parse_content(cls, v: Any) -> List[Any]:
        if isinstance(v, str):
            return [CanonicalTextBlock(text=v)]
        if isinstance(v, dict):
            return [v]
        return v

    @property
    def text_content(self) -> str:

        return "".join(b.text for b in self.content if isinstance(b, CanonicalTextBlock))

    @property
    def thinking_content(self) -> str:

        return "".join(b.thinking for b in self.content if isinstance(b, CanonicalThinkingBlock))

    @property
    def tool_uses(self) -> List[CanonicalToolUseBlock]:

        return [b for b in self.content if isinstance(b, CanonicalToolUseBlock)]

    @property
    def tool_results(self) -> List[CanonicalToolResultBlock]:

        return [b for b in self.content if isinstance(b, CanonicalToolResultBlock)]

class CanonicalUsage(BaseModel):

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class CanonicalChatRequest(BaseModel):

    model: str
    messages: List[CanonicalMessage] = Field(default_factory=list)
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: List[str] = Field(default_factory=list)
    stream: bool = False
    tools: List[ToolDefinition] = Field(default_factory=list)
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    workspace_id: str = Field(default_factory=lambda: get_settings().gateway.default_workspace_id)
    extra_params: Dict[str, Any] = Field(default_factory=dict)

class CanonicalChatResponse(BaseModel):

    id: str
    model: str
    role: Literal["assistant"] = "assistant"
    content: List[CanonicalBlock] = Field(default_factory=list)
    finish_reason: Literal["stop", "tool_use", "max_tokens", "content_filter", "error"] = "stop"
    usage: CanonicalUsage = Field(default_factory=CanonicalUsage)

class CanonicalStreamChunk(BaseModel):

    id: str
    model: str
    delta_content: Optional[str] = None
    delta_thinking: Optional[str] = None
    delta_tool_calls: Optional[List[ToolCall]] = None
    finish_reason: Optional[Literal["stop", "tool_use", "max_tokens", "content_filter", "error"]] = None
    usage: Optional[CanonicalUsage] = None

class CanonicalLLMResponse(BaseModel):

    id: str
    model: str
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: List[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: CanonicalUsage = Field(default_factory=CanonicalUsage)

class CanonicalLLMStreamChunk(BaseModel):

    id: str
    model: str
    delta_content: Optional[str] = None
    delta_reasoning_content: Optional[str] = None
    delta_tool_calls: Optional[List[ToolCall]] = None
    finish_reason: Optional[str] = None
    usage: Optional[CanonicalUsage] = None

class RankedSearchResult(BaseModel):

    id: str
    source_type: Literal["knowledge", "document_chunk"]
    title: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    rank: int
    raw_score: float
    workspace_id: str = Field(default_factory=lambda: get_settings().gateway.default_workspace_id)
    is_global: bool = False
    version: Optional[int] = None

class BlendedSearchResult(BaseModel):

    id: str
    source_type: Literal["knowledge", "document_chunk"]
    title: str
    content: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    rrf_score: float
    normalized_score: float = 0.0
    fts_rank: Optional[int] = None
    vector_rank: Optional[int] = None
    workspace_id: str = Field(default_factory=lambda: get_settings().gateway.default_workspace_id)
    is_global: bool = False
    version: Optional[int] = None
