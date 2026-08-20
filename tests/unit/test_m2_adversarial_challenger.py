"""Unit Adversarial Stress Tests: Milestone M2 Converters, Adapters, and Handlers.

Targeted deep-dive stress testing for:
1. HttpLLMClient URL resolution, header construction, payload serialization, and response parsing.
2. OpenAI and Anthropic converter edge cases (function roles, tool results, complex arguments).
3. AuthValidator and header parsing edge cases.
4. ModelRegistryService alias casing, resolution fallbacks, and listing structures.
"""

import json
import uuid
import pytest

from src.gateway.application.services.model_registry import ModelRegistryService
from src.gateway.domain.canonical import (
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalMessage,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
)
from src.gateway.domain.exceptions import (
    AuthenticationException,
    ModelNotFoundException,
)
from src.gateway.domain.tools import FunctionCall, FunctionDefinition, ToolCall, ToolDefinition
from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient
from src.gateway.presentation.auth import (
    AuthValidator,
    authenticate_credentials,
    authenticate_request,
    is_public_path,
)
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
# 1. HttpLLMClient URL and Header Construction Tests
# ==============================================================================

def test_adv_http_llm_client_url_variations():
    """Verify endpoint resolution across diverse base_url formats."""
    # Standard base url
    c1 = HttpLLMClient(base_url="http://localhost:8000")
    assert c1.endpoint == "http://localhost:8000/v1/chat/completions"
    assert c1.base_url == "http://localhost:8000/v1"

    # Base url with trailing slash
    c2 = HttpLLMClient(base_url="http://localhost:8000/")
    assert c2.endpoint == "http://localhost:8000/v1/chat/completions"

    # Base url with /v1
    c3 = HttpLLMClient(base_url="http://localhost:8000/v1")
    assert c3.endpoint == "http://localhost:8000/v1/chat/completions"
    assert c3.base_url == "http://localhost:8000/v1"

    # Base url with /v1/
    c4 = HttpLLMClient(base_url="http://localhost:8000/v1/")
    assert c4.endpoint == "http://localhost:8000/v1/chat/completions"

    # Full endpoint url
    c5 = HttpLLMClient(base_url="http://localhost:8000/v1/chat/completions")
    assert c5.endpoint == "http://localhost:8000/v1/chat/completions"
    assert c5.base_url == "http://localhost:8000/v1"


def test_adv_http_llm_client_headers():
    """Verify headers with custom, empty, and default API keys."""
    c_empty = HttpLLMClient(api_key="EMPTY")
    headers_empty = c_empty._get_headers()
    assert "Authorization" not in headers_empty

    c_none = HttpLLMClient(api_key="")
    headers_none = c_none._get_headers()
    assert "Authorization" not in headers_none

    c_key = HttpLLMClient(api_key="sk-custom-secret-key")
    headers_key = c_key._get_headers()
    assert headers_key["Authorization"] == "Bearer sk-custom-secret-key"
    assert headers_key["Content-Type"] == "application/json"


# ==============================================================================
# 2. OpenAI Converter Edge Cases
# ==============================================================================

def test_adv_openai_converter_system_and_function_roles():
    """Verify system, assistant, and tool/function roles are correctly mapped to canonical format."""
    req = OpenAIChatCompletionRequest(
        model="gpt-4o",
        messages=[
            OpenAIChatMessage(role="system", content="System instructions"),
            OpenAIChatMessage(role="user", content="Execute function"),
            OpenAIChatMessage(
                role="assistant",
                content=None,
                tool_calls=[
                    OpenAIToolCall(
                        id="call_test_1",
                        type="function",
                        function=OpenAIFunctionCall(name="bash", arguments='{"cmd": "ls"}'),
                    )
                ],
            ),
            OpenAIChatMessage(
                role="tool",
                tool_call_id="call_test_1",
                content="file1.txt\nfile2.txt",
            ),
        ],
    )

    canonical = openai_request_to_canonical(req)
    assert canonical.model == "gpt-4o"
    assert len(canonical.messages) == 4

    # System role
    assert canonical.messages[0].role == "system"
    assert canonical.messages[0].text_content == "System instructions"

    # User message
    assert canonical.messages[1].role == "user"

    # Assistant message with tool call
    assert canonical.messages[2].role == "assistant"
    assert len(canonical.messages[2].tool_uses) == 1
    assert canonical.messages[2].tool_uses[0].name == "bash"
    assert canonical.messages[2].tool_uses[0].input == {"cmd": "ls"}

    # Tool result message
    assert canonical.messages[3].role == "tool"
    assert len(canonical.messages[3].tool_results) == 1
    assert canonical.messages[3].tool_results[0].tool_use_id == "call_test_1"
    assert canonical.messages[3].tool_results[0].content == "file1.txt\nfile2.txt"


def test_adv_openai_response_conversion_with_finish_reason_tool_use():
    """Verify canonical finish_reason 'tool_use' maps to OpenAI 'tool_calls'."""
    canonical_resp = CanonicalChatResponse(
        id="resp-123",
        model="meta-llama/Llama-3.1-8B-Instruct",
        role="assistant",
        content=[
            CanonicalToolUseBlock(
                id="call_abc",
                name="knowledge_search",
                input={"query": "test query"},
            )
        ],
        finish_reason="tool_use",
        usage=CanonicalUsage(prompt_tokens=30, completion_tokens=15, total_tokens=45),
    )

    openai_resp = canonical_response_to_openai(canonical_resp)
    assert openai_resp.choices[0].finish_reason == "tool_calls"
    assert len(openai_resp.choices[0].message.tool_calls) == 1
    tc = openai_resp.choices[0].message.tool_calls[0]
    assert tc.function.name == "knowledge_search"
    assert json.loads(tc.function.arguments) == {"query": "test query"}


# ==============================================================================
# 3. Anthropic Converter Complex Scenarios
# ==============================================================================

def test_adv_anthropic_converter_text_and_tool_results():
    """Verify Anthropic converter correctly handles text, tool_use, and tool result blocks."""
    req = AnthropicMessagesRequest(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1024,
        system="System prompt as string",
        messages=[
            AnthropicMessageParam(
                role="user",
                content=[
                    AnthropicTextBlock(type="text", text="Look at this result:"),
                    AnthropicToolResultBlock(
                        type="tool_result",
                        tool_use_id="toolu_test_99",
                        content="Database query completed successfully.",
                        is_error=False,
                    ),
                ],
            )
        ],
        tools=[
            AnthropicToolParam(
                name="knowledge_search",
                description="Search documents",
                input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
            )
        ],
    )

    canonical = anthropic_request_to_canonical(req)
    assert canonical.system_prompt == "System prompt as string"
    # System prompt is prepended as message 0, user message is message 1
    assert len(canonical.messages) == 2
    assert canonical.messages[0].role == "system"
    assert canonical.messages[0].text_content == "System prompt as string"

    msg = canonical.messages[1]
    assert msg.role == "user"
    assert len(msg.content) == 2

    # Text block
    assert isinstance(msg.content[0], CanonicalTextBlock)
    assert msg.content[0].text == "Look at this result:"

    # Tool result block
    assert isinstance(msg.content[1], CanonicalToolResultBlock)
    assert msg.content[1].tool_use_id == "toolu_test_99"
    assert msg.content[1].content == "Database query completed successfully."
    assert not msg.content[1].is_error

    # Tools
    assert len(canonical.tools) == 1
    assert canonical.tools[0].function.name == "knowledge_search"


def test_adv_anthropic_response_conversion_multiple_blocks():
    """Verify CanonicalChatResponse with both text and tool_use blocks serializes to Anthropic."""
    canonical_resp = CanonicalChatResponse(
        id="msg_987",
        model="claude-3-5-sonnet-20241022",
        role="assistant",
        content=[
            CanonicalTextBlock(text="Let me look that up for you."),
            CanonicalToolUseBlock(
                id="toolu_query_1",
                name="knowledge_get",
                input={"item_id": "12345"},
            ),
        ],
        finish_reason="tool_use",
        usage=CanonicalUsage(prompt_tokens=50, completion_tokens=25, total_tokens=75),
    )

    anthropic_resp = canonical_response_to_anthropic(canonical_resp)
    assert anthropic_resp.id == "msg_987"
    assert anthropic_resp.stop_reason == "tool_use"
    assert len(anthropic_resp.content) == 2
    assert anthropic_resp.content[0].type == "text"
    assert anthropic_resp.content[0].text == "Let me look that up for you."
    assert anthropic_resp.content[1].type == "tool_use"
    assert anthropic_resp.content[1].name == "knowledge_get"
    assert anthropic_resp.content[1].input == {"item_id": "12345"}
    assert anthropic_resp.usage.input_tokens == 50
    assert anthropic_resp.usage.output_tokens == 25


# ==============================================================================
# 4. Model Registry Service Stress Tests
# ==============================================================================

def test_adv_model_registry_casing_and_whitespace():
    """Verify ModelRegistryService handles whitespace, uppercase, and alias resolution."""
    registry = ModelRegistryService(
        default_model="test-default-backend",
        aliases={
            "coding": "Qwen/Qwen2.5-Coder-7B-Instruct",
            "reasoning": "deepseek-ai/DeepSeek-R1-Distill-Qwen-8B",
            "fast": "meta-llama/Llama-3.1-8B-Instruct",
        },
    )

    # Standard exact alias
    assert registry.resolve("coding") == "Qwen/Qwen2.5-Coder-7B-Instruct"

    # Uppercase alias
    assert registry.resolve("CODING") == "Qwen/Qwen2.5-Coder-7B-Instruct"

    # Mixed case with whitespace
    assert registry.resolve("  Reasoning  ") == "deepseek-ai/DeepSeek-R1-Distill-Qwen-8B"

    # Default alias
    assert registry.resolve("default") == "test-default-backend"

    with pytest.raises(ModelNotFoundException):
        registry.resolve("some/unregistered-model")

    # Empty or None resolves to default
    assert registry.resolve("") == "test-default-backend"
    assert registry.resolve(None) == "test-default-backend"


def test_adv_model_registry_listing_format():
    """Verify list_models response list conforms to OpenAI Model List format."""
    registry = ModelRegistryService(
        default_model="meta-llama/Llama-3.1-8B-Instruct",
        aliases={"coding": "Qwen/Qwen2.5-Coder-7B-Instruct"},
    )
    models_list = registry.list_models()
    assert isinstance(models_list, list)
    assert len(models_list) >= 2

    model_ids = [m["id"] for m in models_list]
    assert "coding" in model_ids
    assert "meta-llama/Llama-3.1-8B-Instruct" in model_ids
    assert all(m["object"] == "model" for m in models_list)
    assert all("owned_by" in m for m in models_list)


# ==============================================================================
# 5. Auth Security and Header Parsing Stress Tests
# ==============================================================================

def test_adv_auth_header_parsing_and_timing_defense():
    """Verify auth validator parses Bearer tokens, x-api-key, and rejects bad formats."""
    valid_keys = ["sk-prod-key-1", "sk-prod-key-2"]
    validator = AuthValidator(allowed_keys=valid_keys)

    # Valid Bearer
    assert validator.validate(auth_header="Bearer sk-prod-key-1")

    # Valid lowercase bearer
    assert validator.validate(auth_header="bearer sk-prod-key-2")

    # Valid x-api-key
    assert validator.validate(x_api_key="sk-prod-key-1")

    # Invalid key
    with pytest.raises(AuthenticationException):
        validator.validate(auth_header="Bearer sk-wrong-key")

    # Empty header
    with pytest.raises(AuthenticationException):
        validator.validate(auth_header="", x_api_key="")

    # Malformed Bearer header (no token)
    with pytest.raises(AuthenticationException):
        validator.validate(auth_header="Bearer")

    # Non-Bearer auth scheme (e.g. Basic)
    with pytest.raises(AuthenticationException):
        validator.validate(auth_header="Basic dXNlcjpwYXNz")


def test_adv_public_path_bypass():
    """Verify public path bypass logic for health, docs, and openapi."""
    assert is_public_path("/health")
    assert is_public_path("/health/")
    assert is_public_path("/docs")
    assert is_public_path("/openapi.json")
    assert is_public_path("/redoc")

    # Protected paths must NOT bypass
    assert not is_public_path("/v1/chat/completions")
    assert not is_public_path("/v1/messages")
    assert not is_public_path("/v1/models")
    assert not is_public_path("/v1/files/upload")
