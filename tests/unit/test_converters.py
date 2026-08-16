"""Comprehensive unit tests for OpenAI and Anthropic protocol converters."""

import json
import pytest

from src.gateway.domain.canonical import (
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
)
from src.gateway.domain.tools import FunctionCall, FunctionDefinition, ToolCall, ToolDefinition
from src.gateway.presentation.converters.anthropic_converter import (
    anthropic_request_to_canonical,
    canonical_response_to_anthropic,
)
from src.gateway.presentation.converters.openai_converter import (
    canonical_response_to_openai,
    canonical_stream_chunk_to_openai,
    openai_request_to_canonical,
)
from src.gateway.presentation.schemas.anthropic_schemas import (
    AnthropicMessageParam,
    AnthropicMessagesRequest,
    AnthropicTextBlock,
    AnthropicToolParam,
    AnthropicToolResultBlock,
    AnthropicToolUseBlock,
)
from src.gateway.presentation.schemas.openai_schemas import (
    OpenAIChatCompletionRequest,
    OpenAIChatMessage,
    OpenAIFunctionCall,
    OpenAIFunctionDefinition,
    OpenAIToolCall,
    OpenAIToolDefinition,
)


# ==============================================================================
# OpenAI Converter Tests
# ==============================================================================

def test_openai_request_to_canonical_basic():
    """Verify basic OpenAI request converts to CanonicalChatRequest."""
    req = OpenAIChatCompletionRequest(
        model="gpt-4o",
        messages=[
            OpenAIChatMessage(role="user", content="Hello, world!"),
        ],
        temperature=0.7,
        max_tokens=256,
        top_p=0.9,
        stream=False,
    )

    canonical = openai_request_to_canonical(req)
    assert canonical.model == "gpt-4o"
    assert len(canonical.messages) == 1
    assert canonical.messages[0].role == "user"
    assert canonical.messages[0].text_content == "Hello, world!"
    assert canonical.temperature == 0.7
    assert canonical.max_tokens == 256
    assert canonical.top_p == 0.9
    assert canonical.stream is False
    assert canonical.workspace_id == "global"


def test_openai_request_to_canonical_multi_turn_with_system():
    """Verify multi-turn conversation with system prompt converts properly."""
    req = OpenAIChatCompletionRequest(
        model="coding",
        messages=[
            OpenAIChatMessage(role="system", content="You are a python expert."),
            OpenAIChatMessage(role="user", content="Write a function."),
            OpenAIChatMessage(role="assistant", content="def foo(): pass"),
            OpenAIChatMessage(role="user", content="Add docstring."),
        ],
    )

    canonical = openai_request_to_canonical(req, workspace_id="ws-dev")
    assert canonical.system_prompt == "You are a python expert."
    assert len(canonical.messages) == 4
    assert canonical.messages[0].role == "system"
    assert canonical.messages[1].role == "user"
    assert canonical.messages[2].role == "assistant"
    assert canonical.messages[2].text_content == "def foo(): pass"
    assert canonical.messages[3].role == "user"
    assert canonical.workspace_id == "ws-dev"


def test_openai_request_to_canonical_tools_and_tool_calls():
    """Verify tools and assistant tool_calls convert to Canonical format."""
    tool_def = OpenAIToolDefinition(
        type="function",
        function=OpenAIFunctionDefinition(
            name="get_weather",
            description="Get temperature for city",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        ),
    )

    tc = OpenAIToolCall(
        id="call_abc123",
        type="function",
        function=OpenAIFunctionCall(
            name="get_weather",
            arguments='{"city": "Bangkok"}',
        ),
    )

    req = OpenAIChatCompletionRequest(
        model="gpt-4o",
        messages=[
            OpenAIChatMessage(role="user", content="What is the weather in Bangkok?"),
            OpenAIChatMessage(role="assistant", content=None, tool_calls=[tc]),
            OpenAIChatMessage(
                role="tool",
                content='{"temp": 32}',
                tool_call_id="call_abc123",
            ),
        ],
        tools=[tool_def],
    )

    canonical = openai_request_to_canonical(req)
    assert len(canonical.tools) == 1
    assert canonical.tools[0].function.name == "get_weather"
    assert canonical.tools[0].function.parameters["properties"]["city"]["type"] == "string"

    assert len(canonical.messages) == 3
    # Assistant message has tool use block
    asst_msg = canonical.messages[1]
    assert len(asst_msg.tool_uses) == 1
    assert asst_msg.tool_uses[0].name == "get_weather"
    assert asst_msg.tool_uses[0].input == {"city": "Bangkok"}

    # Tool message has tool result block
    tool_msg = canonical.messages[2]
    assert tool_msg.role == "tool"
    assert len(tool_msg.tool_results) == 1
    assert tool_msg.tool_results[0].tool_use_id == "call_abc123"
    assert tool_msg.tool_results[0].content == '{"temp": 32}'


def test_canonical_response_to_openai_text():
    """Verify text CanonicalChatResponse converts to OpenAIChatCompletionResponse."""
    canonical_resp = CanonicalChatResponse(
        id="chatcmpl-test-01",
        model="meta-llama/Llama-3.1-8B-Instruct",
        role="assistant",
        content=[CanonicalTextBlock(text="Clean Architecture is great.")],
        finish_reason="stop",
        usage=CanonicalUsage(prompt_tokens=15, completion_tokens=8, total_tokens=23),
    )

    openai_resp = canonical_response_to_openai(canonical_resp)
    assert openai_resp.id == "chatcmpl-test-01"
    assert openai_resp.object == "chat.completion"
    assert openai_resp.model == "meta-llama/Llama-3.1-8B-Instruct"
    assert len(openai_resp.choices) == 1
    assert openai_resp.choices[0].finish_reason == "stop"
    assert openai_resp.choices[0].message.role == "assistant"
    assert openai_resp.choices[0].message.content == "Clean Architecture is great."
    assert openai_resp.choices[0].message.tool_calls is None
    assert openai_resp.usage.prompt_tokens == 15
    assert openai_resp.usage.completion_tokens == 8
    assert openai_resp.usage.total_tokens == 23


def test_canonical_response_to_openai_with_tool_calls():
    """Verify CanonicalChatResponse with ToolUseBlocks converts to OpenAI tool_calls payload."""
    canonical_resp = CanonicalChatResponse(
        id="chatcmpl-test-tc",
        model="gpt-4o",
        role="assistant",
        content=[
            CanonicalTextBlock(text="Let me check that."),
            CanonicalToolUseBlock(
                id="call_999",
                name="search_db",
                input={"query": "test query"},
            ),
        ],
        finish_reason="tool_use",
        usage=CanonicalUsage(prompt_tokens=30, completion_tokens=20, total_tokens=50),
    )

    openai_resp = canonical_response_to_openai(canonical_resp)
    assert len(openai_resp.choices) == 1
    choice = openai_resp.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.content == "Let me check that."
    assert choice.message.tool_calls is not None
    assert len(choice.message.tool_calls) == 1
    tc = choice.message.tool_calls[0]
    assert tc.id == "call_999"
    assert tc.function.name == "search_db"
    assert json.loads(tc.function.arguments) == {"query": "test query"}


def test_canonical_stream_chunk_to_openai():
    """Verify streaming CanonicalStreamChunk converts to OpenAIChatCompletionChunk."""
    # 1. Content delta chunk
    chunk1 = CanonicalStreamChunk(
        id="chunk-01",
        model="gpt-4o",
        delta_content="Hello stream",
        finish_reason=None,
    )
    openai_chunk1 = canonical_stream_chunk_to_openai(chunk1)
    assert openai_chunk1.object == "chat.completion.chunk"
    assert openai_chunk1.choices[0].delta.content == "Hello stream"
    assert openai_chunk1.choices[0].finish_reason is None

    # 2. Tool call delta chunk
    chunk2 = CanonicalStreamChunk(
        id="chunk-02",
        model="gpt-4o",
        delta_tool_calls=[
            ToolCall(
                id="call_delta_1",
                type="function",
                function=FunctionCall(name="bash", arguments='{"cmd": "ls"}'),
            )
        ],
        finish_reason="tool_use",
    )
    openai_chunk2 = canonical_stream_chunk_to_openai(chunk2)
    assert openai_chunk2.choices[0].finish_reason == "tool_calls"
    assert openai_chunk2.choices[0].delta.tool_calls is not None
    assert openai_chunk2.choices[0].delta.tool_calls[0]["function"]["name"] == "bash"


# ==============================================================================
# Anthropic Converter Tests
# ==============================================================================

def test_anthropic_request_to_canonical_basic():
    """Verify basic Anthropic request converts to CanonicalChatRequest."""
    req = AnthropicMessagesRequest(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1024,
        system="You are a helpful coding assistant.",
        messages=[
            AnthropicMessageParam(role="user", content="Hello Anthropic"),
        ],
        temperature=0.5,
    )

    canonical = anthropic_request_to_canonical(req, workspace_id="ws-claude")
    assert canonical.model == "claude-3-5-sonnet-20241022"
    assert canonical.system_prompt == "You are a helpful coding assistant."
    assert len(canonical.messages) == 2  # system + user
    assert canonical.messages[0].role == "system"
    assert canonical.messages[0].text_content == "You are a helpful coding assistant."
    assert canonical.messages[1].role == "user"
    assert canonical.messages[1].text_content == "Hello Anthropic"
    assert canonical.max_tokens == 1024
    assert canonical.temperature == 0.5
    assert canonical.workspace_id == "ws-claude"


def test_anthropic_request_to_canonical_system_blocks():
    """Verify Anthropic system parameter as list of text blocks converts properly."""
    req = AnthropicMessagesRequest(
        model="claude-3-5-sonnet",
        system=[
            AnthropicTextBlock(type="text", text="System Rule 1"),
            AnthropicTextBlock(type="text", text="System Rule 2"),
        ],
        messages=[
            AnthropicMessageParam(role="user", content="Test"),
        ],
    )

    canonical = anthropic_request_to_canonical(req)
    assert canonical.system_prompt == "System Rule 1\n\nSystem Rule 2"


def test_anthropic_request_to_canonical_tools_and_blocks():
    """Verify Anthropic tools, tool_use blocks, and tool_result blocks convert to Canonical."""
    tool_param = AnthropicToolParam(
        name="knowledge_search",
        description="Search knowledge base",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )

    req = AnthropicMessagesRequest(
        model="claude-3-5-sonnet",
        messages=[
            AnthropicMessageParam(role="user", content="Find architecture docs"),
            AnthropicMessageParam(
                role="assistant",
                content=[
                    AnthropicTextBlock(type="text", text="Searching..."),
                    AnthropicToolUseBlock(
                        type="tool_use",
                        id="toolu_001",
                        name="knowledge_search",
                        input={"query": "architecture"},
                    ),
                ],
            ),
            AnthropicMessageParam(
                role="user",
                content=[
                    AnthropicToolResultBlock(
                        type="tool_result",
                        tool_use_id="toolu_001",
                        content="Found 3 results.",
                        is_error=False,
                    )
                ],
            ),
        ],
        tools=[tool_param],
    )

    canonical = anthropic_request_to_canonical(req)
    assert len(canonical.tools) == 1
    assert canonical.tools[0].function.name == "knowledge_search"
    assert canonical.tools[0].function.parameters["properties"]["query"]["type"] == "string"

    assert len(canonical.messages) == 3
    # Assistant message has tool use
    asst_msg = canonical.messages[1]
    assert len(asst_msg.tool_uses) == 1
    assert asst_msg.tool_uses[0].name == "knowledge_search"
    assert asst_msg.tool_uses[0].input == {"query": "architecture"}

    # User message has tool result
    user_msg2 = canonical.messages[2]
    assert len(user_msg2.tool_results) == 1
    assert user_msg2.tool_results[0].tool_use_id == "toolu_001"
    assert user_msg2.tool_results[0].content == "Found 3 results."


def test_canonical_response_to_anthropic_text():
    """Verify CanonicalChatResponse converts to AnthropicMessagesResponse."""
    canonical_resp = CanonicalChatResponse(
        id="msg_test_01",
        model="claude-3-5-sonnet",
        role="assistant",
        content=[CanonicalTextBlock(text="Here is your response.")],
        finish_reason="stop",
        usage=CanonicalUsage(prompt_tokens=42, completion_tokens=18, total_tokens=60),
    )

    anthropic_resp = canonical_response_to_anthropic(canonical_resp)
    assert anthropic_resp.id == "msg_test_01"
    assert anthropic_resp.type == "message"
    assert anthropic_resp.role == "assistant"
    assert anthropic_resp.stop_reason == "end_turn"
    assert len(anthropic_resp.content) == 1
    assert anthropic_resp.content[0].type == "text"  # type: ignore[union-attr]
    assert anthropic_resp.content[0].text == "Here is your response."  # type: ignore[union-attr]
    assert anthropic_resp.usage.input_tokens == 42
    assert anthropic_resp.usage.output_tokens == 18


def test_canonical_response_to_anthropic_tool_use():
    """Verify CanonicalChatResponse with tool use converts to Anthropic tool_use content block."""
    canonical_resp = CanonicalChatResponse(
        id="msg_test_tc",
        model="claude-3-5-sonnet",
        role="assistant",
        content=[
            CanonicalTextBlock(text="Calling tool now."),
            CanonicalToolUseBlock(
                id="toolu_calc_1",
                name="calculator",
                input={"expr": "2 + 2"},
            ),
        ],
        finish_reason="tool_use",
        usage=CanonicalUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
    )

    anthropic_resp = canonical_response_to_anthropic(canonical_resp)
    assert anthropic_resp.stop_reason == "tool_use"
    assert len(anthropic_resp.content) == 2
    assert anthropic_resp.content[0].type == "text"  # type: ignore[union-attr]
    assert anthropic_resp.content[1].type == "tool_use"  # type: ignore[union-attr]
    assert anthropic_resp.content[1].name == "calculator"  # type: ignore[union-attr]
    assert anthropic_resp.content[1].input == {"expr": "2 + 2"}  # type: ignore[union-attr]
