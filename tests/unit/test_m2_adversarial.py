"""Adversarial stress-test suite for Milestone M2.

Challenges:
1. Auth security: timing, header variations, null bytes, unicode, public path boundary, 401 format.
2. Model registry: case insensitivity, whitespace, unicode, fallback, dynamic registration, discovery.
3. OpenAI converter: malformed JSON in tool calls, multi-modal content, system merging, streaming chunks.
4. Anthropic converter: block variations, tool result errors, system blocks, event sequence generation.
5. Router integration: SSE stream generation, protocol fidelity, error bubbling.
"""

import json
import uuid
import pytest
from typing import Any, AsyncIterator, Dict, List, Optional
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.config import settings
from src.gateway.application.services.model_registry import (
    ModelRegistryService,
    get_model_registry,
)
from src.gateway.domain.canonical import (
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
)
from src.gateway.domain.exceptions import (
    AuthenticationException,
    ModelNotFoundException,
)
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.domain.tools import FunctionCall, FunctionDefinition, ToolCall, ToolDefinition
from src.gateway.presentation.auth import (
    APIKeyAuthMiddleware,
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
from src.gateway.presentation.routers.chat_completions import (
    _canonical_to_upstream_messages,
    _canonical_to_upstream_tools,
    get_llm_client as get_chat_llm_client,
    router as chat_router,
)
from src.gateway.presentation.routers.messages import (
    _canonical_to_upstream_payload,
    get_llm_client as get_messages_llm_client,
    router as messages_router,
)
from src.gateway.presentation.routers.models import router as models_router
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


def _authorize_test_app(app: FastAPI) -> None:
    @app.middleware("http")
    async def assign_principal(request, call_next):
        request.state.principal = Principal(
            subject_id="router-test",
            kind=PrincipalKind.API_KEY,
            system_role=SystemRole.MEMBER,
            scopes=frozenset({"chat:write"}),
        )
        return await call_next(request)


# ==============================================================================
# 1. AUTHENTICATION ADVERSARIAL CHALLENGES
# ==============================================================================

class TestAuthAdversarial:
    """Stress-test authentication logic and edge cases."""

    def test_bearer_case_and_whitespace_variations(self):
        allowed = ["sk-test-key-12345"]

        # Mixed casing of Bearer
        assert authenticate_credentials("bearer sk-test-key-12345", allowed_keys=allowed) == "sk-test-key-12345"
        assert authenticate_credentials("BEARER sk-test-key-12345", allowed_keys=allowed) == "sk-test-key-12345"
        assert authenticate_credentials("BeArEr sk-test-key-12345", allowed_keys=allowed) == "sk-test-key-12345"

        # Multiple spaces between prefix and token
        assert authenticate_credentials("Bearer       sk-test-key-12345", allowed_keys=allowed) == "sk-test-key-12345"
        assert authenticate_credentials("  Bearer   sk-test-key-12345  ", allowed_keys=allowed) == "sk-test-key-12345"

    def test_malformed_auth_headers_fail(self):
        allowed = ["sk-test-key-12345"]

        # Missing token part
        with pytest.raises(AuthenticationException):
            authenticate_credentials("Bearer", allowed_keys=allowed)

        with pytest.raises(AuthenticationException):
            authenticate_credentials("Bearer ", allowed_keys=allowed)

        with pytest.raises(AuthenticationException):
            authenticate_credentials("Bearer      ", allowed_keys=allowed)

        # Wrong prefix (e.g. Basic, Token, Digest)
        with pytest.raises(AuthenticationException):
            authenticate_credentials("Basic dXNlcjpwYXNz", allowed_keys=allowed)

        with pytest.raises(AuthenticationException):
            authenticate_credentials("Token sk-test-key-12345", allowed_keys=allowed)

        # Null bytes injection attempt
        with pytest.raises(AuthenticationException):
            authenticate_credentials("Bearer sk-test-key-12345\x00extra", allowed_keys=allowed)

        with pytest.raises(AuthenticationException):
            authenticate_credentials(x_api_key="sk-test-key-12345\x00injected", allowed_keys=allowed)

    def test_unicode_and_special_character_keys(self):
        unicode_keys = ["sk-🔑-master-key", "sk-キー-tokyo-2026", "sk-!@#$%^&*()_+~"]

        # Valid match with unicode
        assert authenticate_credentials("Bearer sk-🔑-master-key", allowed_keys=unicode_keys) == "sk-🔑-master-key"
        assert authenticate_credentials(x_api_key="sk-キー-tokyo-2026", allowed_keys=unicode_keys) == "sk-キー-tokyo-2026"
        assert authenticate_credentials("Bearer sk-!@#$%^&*()_+~", allowed_keys=unicode_keys) == "sk-!@#$%^&*()_+~"

        # Tampered unicode fails
        with pytest.raises(AuthenticationException):
            authenticate_credentials("Bearer sk-🗝️-master-key", allowed_keys=unicode_keys)

    def test_dual_headers_fallback_and_precedence(self):
        allowed = ["sk-valid-primary", "sk-valid-secondary"]

        # Case 1: Invalid Bearer but Valid x-api-key -> Should succeed via x-api-key
        token = authenticate_request(
            {"Authorization": "InvalidFormat", "x-api-key": "sk-valid-secondary"},
            allowed_keys=allowed,
        )
        assert token == "sk-valid-secondary"

        # Case 2: Valid Bearer and Valid x-api-key -> Bearer is chosen first
        token2 = authenticate_request(
            {"Authorization": "Bearer sk-valid-primary", "x-api-key": "sk-valid-secondary"},
            allowed_keys=allowed,
        )
        assert token2 == "sk-valid-primary"

        # Case 3: Both invalid -> Raises AuthenticationException
        with pytest.raises(AuthenticationException):
            authenticate_request(
                {"Authorization": "Bearer sk-wrong-1", "x-api-key": "sk-wrong-2"},
                allowed_keys=allowed,
            )

    def test_empty_allowed_keys_configuration(self):
        # When no keys are allowed, all auth attempts must be rejected
        with pytest.raises(AuthenticationException):
            authenticate_credentials("Bearer sk-some-key", allowed_keys=[])

        with pytest.raises(AuthenticationException):
            authenticate_credentials(x_api_key="sk-some-key", allowed_keys=[])

    def test_public_path_boundary_conditions(self):
        # True public paths
        assert is_public_path("/health") is True
        assert is_public_path("/health/") is True
        assert is_public_path("/docs") is True
        assert is_public_path("/docs/oauth2-redirect") is True
        assert is_public_path("/openapi.json") is True
        assert is_public_path("/redoc") is True
        assert is_public_path("/favicon.ico") is True

        # Non-public API paths MUST NOT be bypassed
        assert is_public_path("/v1/chat/completions") is False
        assert is_public_path("/v1/messages") is False
        assert is_public_path("/v1/models") is False
        assert is_public_path("/v1/files/upload") is False
        assert is_public_path("/api/knowledge") is False

    def test_auth_middleware_401_format_by_route(self):
        app = FastAPI()
        app.add_middleware(APIKeyAuthMiddleware, allowed_keys=["sk-valid-secret"])

        @app.post("/v1/chat/completions")
        def chat():
            return {"ok": True}

        @app.post("/v1/messages")
        def messages():
            return {"ok": True}

        @app.get("/v1/models")
        def models():
            return {"ok": True}

        client = TestClient(app)

        # 1. Chat completions 401 returns OpenAI error format
        resp_chat = client.post("/v1/chat/completions")
        assert resp_chat.status_code == 401
        data_chat = resp_chat.json()
        assert "error" in data_chat
        assert data_chat["error"]["code"] == "invalid_api_key"

        # 2. Messages 401 returns Anthropic error format
        resp_msg = client.post("/v1/messages")
        assert resp_msg.status_code == 401
        data_msg = resp_msg.json()
        assert data_msg.get("type") == "error"
        assert data_msg["error"]["type"] == "authentication_error"

        # 3. Models 401 returns OpenAI default format
        resp_models = client.get("/v1/models")
        assert resp_models.status_code == 401
        assert "error" in resp_models.json()


# ==============================================================================
# 2. MODEL REGISTRY ADVERSARIAL CHALLENGES
# ==============================================================================

class TestModelRegistryAdversarial:
    """Stress-test Model Registry aliases, resolution, and edge cases."""

    def test_alias_resolution_edge_cases(self):
        registry = ModelRegistryService(
            default_model="test-default-backend",
            aliases={
                "custom_alias": "custom/target-model-v1",
                "UPPER_ALIAS": "target/upper-model",
            },
        )

        assert registry.resolve("CoDiNg") == "test-default-backend"
        assert registry.resolve("rEaSoNiNg") == "test-default-backend"
        assert registry.resolve("FaSt") == "test-default-backend"
        assert registry.resolve("CUSTOM_ALIAS") == "custom/target-model-v1"
        assert registry.resolve("upper_alias") == "target/upper-model"

        # Surrounding whitespace
        assert registry.resolve("  coding  ") == "test-default-backend"
        with pytest.raises(ModelNotFoundException):
            registry.resolve("\tunknown-model\n")
        assert registry.resolve("   ") == "test-default-backend"
        assert registry.resolve(None) == "test-default-backend"

    def test_unicode_and_special_char_models(self):
        registry = ModelRegistryService(default_model="test-default-backend")
        unicode_model = "org/モデル-large-v2:q4_k_m"
        with pytest.raises(ModelNotFoundException):
            registry.resolve(unicode_model)

        # Register alias with special characters
        registry.register_alias("code/py:v1", "org/backend-py-model")
        assert registry.resolve("code/py:v1") == "org/backend-py-model"
        assert registry.resolve("CODE/PY:V1") == "org/backend-py-model"

    def test_get_model_info_alias_root_and_metadata(self):
        registry = ModelRegistryService(default_model="test-default-backend")

        # Alias info should have root pointing to resolved backend model
        info_default = registry.get_model_info("default")
        assert info_default["id"] == "default"
        assert info_default["root"] == "test-default-backend"
        assert info_default["context_window"] == settings.llm.context_window

        # Backend model info should have root=None
        info_backend = registry.get_model_info("test-default-backend")
        assert info_backend["id"] == "test-default-backend"
        assert info_backend["root"] is None
        assert info_backend["context_window"] == settings.llm.context_window

        with pytest.raises(ModelNotFoundException):
            registry.get_model_info("my-unregistered-model")

    def test_list_models_contains_backends_and_aliases(self):
        registry = ModelRegistryService(default_model="test-default-backend")
        models = registry.list_models()
        ids = [m["id"] for m in models]

        assert "test-default-backend" in ids
        assert "default" in ids
        assert "coding" in ids
        assert "reasoning" in ids
        assert "fast" in ids


# ==============================================================================
# 3. OPENAI CONVERTER ADVERSARIAL CHALLENGES
# ==============================================================================

class TestOpenAIConverterAdversarial:
    """Stress-test OpenAI protocol request/response/stream conversion."""

    def test_malformed_tool_call_json_arguments_graceful_fallback(self):
        # When model returns malformed JSON in tool call arguments
        tc = OpenAIToolCall(
            id="call_corrupt",
            type="function",
            function=OpenAIFunctionCall(
                name="bash",
                arguments='{"cmd": "ls -la", unclosed_json...',
            ),
        )

        req = OpenAIChatCompletionRequest(
            model="gpt-4o",
            messages=[
                OpenAIChatMessage(role="assistant", content=None, tool_calls=[tc]),
            ],
        )

        canonical = openai_request_to_canonical(req)
        assert len(canonical.messages) == 1
        asst = canonical.messages[0]
        assert len(asst.tool_uses) == 1
        assert asst.tool_uses[0].name == "bash"
        # Should store raw arguments without raising an exception
        assert "raw_arguments" in asst.tool_uses[0].input

    def test_multi_system_messages_merging(self):
        req = OpenAIChatCompletionRequest(
            model="coding",
            messages=[
                OpenAIChatMessage(role="system", content="Rule 1: Always use types."),
                OpenAIChatMessage(role="system", content="Rule 2: Keep functions short."),
                OpenAIChatMessage(role="user", content="Write a function."),
            ],
        )

        canonical = openai_request_to_canonical(req)
        assert canonical.system_prompt == "Rule 1: Always use types.\n\nRule 2: Keep functions short."
        assert len(canonical.messages) == 3

    def test_multimodal_content_parts_extraction(self):
        req = OpenAIChatCompletionRequest(
            model="gpt-4o",
            messages=[
                OpenAIChatMessage(
                    role="user",
                    content=[
                        {"type": "text", "text": "Part 1 of prompt."},
                        {"type": "text", "text": " Part 2 of prompt."},
                    ],
                )
            ],
        )

        canonical = openai_request_to_canonical(req)
        assert len(canonical.messages) == 1
        user_msg = canonical.messages[0]
        assert user_msg.text_content == "Part 1 of prompt. Part 2 of prompt."

    def test_empty_content_and_none_fields(self):
        req = OpenAIChatCompletionRequest(
            model="gpt-4o",
            messages=[
                OpenAIChatMessage(role="user", content=None),
                OpenAIChatMessage(role="assistant", content=""),
            ],
        )

        canonical = openai_request_to_canonical(req)
        assert len(canonical.messages) == 2
        assert canonical.messages[0].text_content == ""
        assert canonical.messages[1].text_content == ""

    def test_tool_results_with_string_and_json_content(self):
        req = OpenAIChatCompletionRequest(
            model="gpt-4o",
            messages=[
                OpenAIChatMessage(
                    role="tool",
                    tool_call_id="call_01",
                    content='{"status": "ok", "items": [1, 2, 3]}',
                ),
            ],
        )

        canonical = openai_request_to_canonical(req)
        tool_msg = canonical.messages[0]
        assert tool_msg.role == "tool"
        assert len(tool_msg.tool_results) == 1
        assert "status" in tool_msg.tool_results[0].content

    def test_canonical_response_finish_reasons(self):
        # 1. stop
        resp1 = CanonicalChatResponse(
            id="resp1", model="test", role="assistant",
            content=[CanonicalTextBlock(text="Done")],
            finish_reason="stop",
        )
        assert canonical_response_to_openai(resp1).choices[0].finish_reason == "stop"

        # 2. length / max_tokens -> canonical max_tokens maps to OpenAI 'length'
        resp2 = CanonicalChatResponse(
            id="resp2", model="test", role="assistant",
            content=[CanonicalTextBlock(text="Truncated")],
            finish_reason="max_tokens",
        )
        assert canonical_response_to_openai(resp2).choices[0].finish_reason == "length"

        # 3. tool_use -> maps to tool_calls in OpenAI
        resp3 = CanonicalChatResponse(
            id="resp3", model="test", role="assistant",
            content=[CanonicalToolUseBlock(id="call_x", name="fn", input={})],
            finish_reason="tool_use",
        )
        assert canonical_response_to_openai(resp3).choices[0].finish_reason == "tool_calls"


# ==============================================================================
# 4. ANTHROPIC CONVERTER ADVERSARIAL CHALLENGES
# ==============================================================================

class TestAnthropicConverterAdversarial:
    """Stress-test Anthropic protocol request/response conversion."""

    def test_system_prompt_variations(self):
        # String system
        req1 = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",
            system="System string",
            messages=[AnthropicMessageParam(role="user", content="Hi")],
        )
        assert anthropic_request_to_canonical(req1).system_prompt == "System string"

        # List of blocks
        req2 = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",
            system=[
                AnthropicTextBlock(type="text", text="Block 1"),
                AnthropicTextBlock(type="text", text="Block 2"),
            ],
            messages=[AnthropicMessageParam(role="user", content="Hi")],
        )
        assert anthropic_request_to_canonical(req2).system_prompt == "Block 1\n\nBlock 2"

        # None system
        req3 = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",
            system=None,
            messages=[AnthropicMessageParam(role="user", content="Hi")],
        )
        assert anthropic_request_to_canonical(req3).system_prompt is None

    def test_tool_result_is_error_flag_preservation(self):
        req = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",
            messages=[
                AnthropicMessageParam(
                    role="user",
                    content=[
                        AnthropicToolResultBlock(
                            type="tool_result",
                            tool_use_id="toolu_err",
                            content="Command failed with exit code 1",
                            is_error=True,
                        )
                    ],
                )
            ],
        )

        canonical = anthropic_request_to_canonical(req)
        assert len(canonical.messages) == 1
        user_msg = canonical.messages[0]
        assert len(user_msg.tool_results) == 1
        assert user_msg.tool_results[0].is_error is True
        assert user_msg.tool_results[0].content == "Command failed with exit code 1"

    def test_raw_dict_blocks_in_anthropic_messages(self):
        # Clients sending raw dicts instead of structured pydantic classes
        req = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",
            messages=[
                AnthropicMessageParam(
                    role="user",
                    content=[
                        {"type": "text", "text": "Raw dict text"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_dict_1",
                            "content": "Dict result content",
                            "is_error": False,
                        },
                    ],
                )
            ],
        )

        canonical = anthropic_request_to_canonical(req)
        user_msg = canonical.messages[0]
        assert len(user_msg.content) == 2
        assert isinstance(user_msg.content[0], CanonicalTextBlock)
        assert user_msg.content[0].text == "Raw dict text"
        assert isinstance(user_msg.content[1], CanonicalToolResultBlock)
        assert user_msg.content[1].tool_use_id == "toolu_dict_1"

    def test_empty_content_fallback_in_response(self):
        # Empty content should produce at least one text block with empty string
        canonical_resp = CanonicalChatResponse(
            id="msg_empty",
            model="claude-3-5-sonnet",
            role="assistant",
            content=[],
            finish_reason="stop",
        )

        anthropic_resp = canonical_response_to_anthropic(canonical_resp)
        assert len(anthropic_resp.content) == 1
        assert anthropic_resp.content[0].type == "text"  # type: ignore[union-attr]
        assert anthropic_resp.content[0].text == ""  # type: ignore[union-attr]


# ==============================================================================
# 5. END-TO-END FASTAPI ROUTER ADVERSARIAL CHALLENGES (MOCK LLM)
# ==============================================================================

class MockAdversarialLLMClient(ILLMClient):
    """Mock LLM client simulating normal, streaming, tool calls, and error conditions."""

    def __init__(self, mode: str = "text"):
        self.mode = mode
        self.last_generate_call: Optional[Dict[str, Any]] = None

    async def generate(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        model: str = "default",
        **kwargs: Any,
    ) -> CanonicalLLMResponse:
        self.last_generate_call = {
            "messages": messages,
            "tools": tools,
            "model": model,
            "kwargs": kwargs,
        }

        if self.mode == "text":
            return CanonicalLLMResponse(
                id="mock-resp-text-1",
                model=model,
                content="Hello from mock LLM!",
                tool_calls=[],
                finish_reason="stop",
                usage=CanonicalUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )
        elif self.mode == "tool_call":
            return CanonicalLLMResponse(
                id="mock-resp-tc-1",
                model=model,
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_mock_1",
                        type="function",
                        function=FunctionCall(name="bash", arguments='{"cmd": "echo 42"}'),
                    )
                ],
                finish_reason="tool_use",
                usage=CanonicalUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
            )
        elif self.mode == "error":
            raise RuntimeError("Simulated upstream provider failure 500")

        raise ValueError(f"Unknown mode {self.mode}")

    async def generate_stream(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        model: str = "default",
        **kwargs: Any,
    ) -> AsyncIterator[CanonicalLLMStreamChunk]:
        if self.mode == "stream_text":
            tokens = ["Hello", " ", "streaming", " ", "world!"]
            for idx, token in enumerate(tokens):
                is_last = idx == len(tokens) - 1
                yield CanonicalLLMStreamChunk(
                    id="chunk-stream",
                    model=model,
                    delta_content=token,
                    delta_tool_calls=None,
                    finish_reason="stop" if is_last else None,
                )
        elif self.mode == "stream_tool_call":
            yield CanonicalLLMStreamChunk(
                id="chunk-tc-start",
                model=model,
                delta_content="",
                delta_tool_calls=[
                    ToolCall(
                        id="call_stream_1",
                        type="function",
                        function=FunctionCall(name="web_search", arguments='{"query": "architecture"}'),
                    )
                ],
                finish_reason="tool_use",
            )
        elif self.mode == "stream_error":
            yield CanonicalLLMStreamChunk(id="chunk-1", model=model, delta_content="Start...")
            raise RuntimeError("Stream failed mid-flight")


class TestRouterAdversarial:
    """Stress-test FastAPI routers with full HTTP pipeline."""

    def test_openai_chat_completions_alias_resolution_in_route(self):
        mock_llm = MockAdversarialLLMClient(mode="text")
        app = FastAPI()
        _authorize_test_app(app)
        app.include_router(chat_router)
        app.dependency_overrides[get_chat_llm_client] = lambda: mock_llm
        app.dependency_overrides[get_model_registry] = lambda: ModelRegistryService(
            default_model="test-default-backend"
        )

        client = TestClient(app)

        # Request using a former built-in alias name; resolves to the default model
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "coding",
                "messages": [{"role": "user", "content": "Write tests"}],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["message"]["content"] == "Hello from mock LLM!"

        # Verify LLM client received resolved backend model
        assert mock_llm.last_generate_call is not None
        assert mock_llm.last_generate_call["model"] == "test-default-backend"

    def test_openai_chat_completions_streaming_sse_format(self):
        mock_llm = MockAdversarialLLMClient(mode="stream_text")
        app = FastAPI()
        _authorize_test_app(app)
        app.include_router(chat_router)
        app.dependency_overrides[get_chat_llm_client] = lambda: mock_llm

        client = TestClient(app)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "fast",
                "messages": [{"role": "user", "content": "Stream me"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        body_text = response.text
        lines = [line.strip() for line in body_text.split("\n") if line.strip()]

        # All lines should start with data:
        for line in lines:
            assert line.startswith("data: ")

        # Last line must be data: [DONE]
        assert lines[-1] == "data: [DONE]"

        # Collect streamed text
        collected = []
        for line in lines[:-1]:
            payload = json.loads(line[6:])
            content = payload["choices"][0]["delta"].get("content")
            if content:
                collected.append(content)

        assert "".join(collected) == "Hello streaming world!"

    def test_anthropic_messages_streaming_sse_events_sequence(self):
        mock_llm = MockAdversarialLLMClient(mode="stream_text")
        app = FastAPI()
        _authorize_test_app(app)
        app.include_router(messages_router)
        app.dependency_overrides[get_messages_llm_client] = lambda: mock_llm

        client = TestClient(app)

        response = client.post(
            "/v1/messages",
            json={
                "model": "reasoning",
                "messages": [{"role": "user", "content": "Tell me a thought"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        raw_events = response.text.strip().split("\n\n")
        event_types = []
        for raw in raw_events:
            lines = raw.strip().split("\n")
            ev_type = None
            for line in lines:
                if line.startswith("event: "):
                    ev_type = line[7:]
            if ev_type:
                event_types.append(ev_type)

        # Expected event sequence: message_start -> content_block_start -> content_block_delta* -> content_block_stop -> message_delta -> message_stop
        assert event_types[0] == "message_start"
        assert event_types[1] == "content_block_start"
        assert "content_block_delta" in event_types
        assert "content_block_stop" in event_types
        assert "message_delta" in event_types
        assert event_types[-1] == "message_stop"

    def test_anthropic_messages_streaming_tool_call_sequence(self):
        mock_llm = MockAdversarialLLMClient(mode="stream_tool_call")
        app = FastAPI()
        _authorize_test_app(app)
        app.include_router(messages_router)
        app.dependency_overrides[get_messages_llm_client] = lambda: mock_llm

        client = TestClient(app)

        response = client.post(
            "/v1/messages",
            json={
                "model": "coding",
                "messages": [{"role": "user", "content": "Search docs"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        raw_events = response.text.strip().split("\n\n")
        event_types = []
        for raw in raw_events:
            lines = raw.strip().split("\n")
            ev_type = None
            for line in lines:
                if line.startswith("event: "):
                    ev_type = line[7:]
            if ev_type:
                event_types.append(ev_type)

        assert "content_block_start" in event_types
        assert "content_block_delta" in event_types
        assert "content_block_stop" in event_types
        assert "message_delta" in event_types
        assert event_types[-1] == "message_stop"

    def test_anthropic_messages_streaming_error_event(self):
        mock_llm = MockAdversarialLLMClient(mode="stream_error")
        app = FastAPI()
        _authorize_test_app(app)
        app.include_router(messages_router)
        app.dependency_overrides[get_messages_llm_client] = lambda: mock_llm

        client = TestClient(app)

        response = client.post(
            "/v1/messages",
            json={
                "model": "coding",
                "messages": [{"role": "user", "content": "Trigger error"}],
                "stream": True,
            },
        )
        assert response.status_code == 200
        assert "event: error" in response.text

    def test_models_router_get_model_with_slash_path(self):
        app = FastAPI()
        _authorize_test_app(app)
        app.include_router(models_router)
        app.dependency_overrides[get_model_registry] = lambda: ModelRegistryService(
            default_model="org/backend-model-v1"
        )

        client = TestClient(app)

        # Model with slash (the configured default backend model)
        response = client.get("/v1/models/org/backend-model-v1")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "org/backend-model-v1"
        assert data["context_window"] == settings.llm.context_window

        # Alias model
        resp_alias = client.get("/v1/models/default")
        assert resp_alias.status_code == 200
        data_alias = resp_alias.json()
        assert data_alias["id"] == "default"
        assert data_alias["root"] == "org/backend-model-v1"
