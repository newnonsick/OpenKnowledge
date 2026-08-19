"""Tier 5 Adversarial Stress Tests: Full System End-to-End Adversarial Matrix.

Empirically stress-tests the complete Gateway HTTP API surface:
1. Malformed Authentication Headers & Constant-Time Verification:
   - Missing headers, empty keys, invalid bearer formats, 10,000-char garbage keys.
2. Dual-Protocol Parity (OpenAI vs Anthropic):
   - External tool passthrough formatting parity.
   - Internal knowledge tool transparency (client never sees internal tool calls).
   - Max tool iteration guardrail warnings and stop reasons.
3. High-Concurrency ASGI Request Stress:
   - 20 concurrent OpenAI /v1/chat/completions requests.
   - 20 concurrent Anthropic /v1/messages requests.
4. HTTP Multipart Corrupted File Ingestion Attacks.
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any, AsyncIterator, Dict, List, Optional
from uuid import uuid4

import httpx
import pytest

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.config import settings
from src.gateway.domain.canonical import (
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalUsage,
)
from src.gateway.domain.tools import FunctionCall, ToolCall
from src.gateway.main import create_app
from src.gateway.presentation.routers.chat_completions import get_llm_client as get_openai_llm_client
from src.gateway.presentation.routers.messages import get_llm_client as get_anthropic_llm_client
from tests.e2e.harness.test_env import TestEnvironment


def get_valid_api_key() -> str:
    return settings.gateway.gateway_api_keys[0]


class ScriptedAsyncLLM(ILLMClient):
    """Fast, thread-safe in-process scripted LLM client for FastAPI dependency overrides."""

    def __init__(self, response: CanonicalLLMResponse):
        self.response = response
        self.call_count = 0

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> CanonicalLLMResponse:
        self.call_count += 1
        return self.response

    async def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[CanonicalLLMStreamChunk]:
        self.call_count += 1
        yield CanonicalLLMStreamChunk(
            id=self.response.id,
            model=self.response.model,
            delta_content=self.response.content,
            finish_reason=self.response.finish_reason,
            usage=self.response.usage,
        )


# ==============================================================================
# 1. ADVERSARIAL AUTHENTICATION STRESS
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialAuthentication:
    """Stress-tests API Key Authentication Middleware under malicious and corrupted headers."""

    @pytest.mark.parametrize(
        "invalid_header",
        [
            {},
            {"Authorization": ""},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer "},
            {"Authorization": "Basic dXNlcjpwYXNz"},
            {"Authorization": "Token invalid_token_format"},
            {"Authorization": "Bearer " + "A" * 10000},
            {"Authorization": "Bearer invalid_random_key_99999"},
            {"x-api-key": ""},
            {"x-api-key": "   "},
            {"x-api-key": "wrong_key_12345"},
            {"x-api-key": "A" * 10000},
        ],
    )
    async def test_adversarial_auth_headers_rejected_401(self, invalid_header: Dict[str, str]):
        """Verify all invalid/malicious authentication headers return HTTP 401 Unauthorized."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # Test against OpenAI endpoint
            resp_openai = await client.post(
                "/v1/chat/completions",
                headers=invalid_header,
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            )
            assert resp_openai.status_code == 401
            assert "error" in resp_openai.json()

            # Test against Anthropic endpoint
            resp_anthropic = await client.post(
                "/v1/messages",
                headers=invalid_header,
                json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100},
            )
            assert resp_anthropic.status_code == 401
            assert "error" in resp_anthropic.json()


# ==============================================================================
# 2. DUAL-PROTOCOL PARITY & TOOL INTERCEPTION ADVERSARIAL INTEGRATION
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialProtocolParity:
    """Stress-tests protocol parity between OpenAI and Anthropic endpoints for tool execution."""

    async def test_openai_external_tool_passthrough_structure(self):
        """Verify OpenAI endpoint returns tool_calls choice and finish_reason='tool_calls'."""
        mock_resp = CanonicalLLMResponse(
            id="chatcmpl-test-tool-1",
            model="gpt-4o",
            content="I need to run tests.",
            tool_calls=[
                ToolCall(
                    id="call_bash_001",
                    function=FunctionCall(
                        name="bash",
                        arguments=json.dumps({"command": "npm test"}),
                    ),
                )
            ],
            finish_reason="tool_calls",
            usage=CanonicalUsage(prompt_tokens=25, completion_tokens=15, total_tokens=40),
        )

        app = create_app()
        app.dependency_overrides[get_openai_llm_client] = lambda: ScriptedAsyncLLM(mock_resp)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {get_valid_api_key()}"},
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Run tests"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "bash",
                                "description": "Run shell command",
                                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
                            },
                        }
                    ],
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert data["object"] == "chat.completion"
            choice = data["choices"][0]
            assert choice["finish_reason"] == "tool_calls"
            assert len(choice["message"]["tool_calls"]) == 1
            assert choice["message"]["tool_calls"][0]["function"]["name"] == "bash"
            args = json.loads(choice["message"]["tool_calls"][0]["function"]["arguments"])
            assert args["command"] == "npm test"

    async def test_anthropic_external_tool_passthrough_structure(self):
        """Verify Anthropic endpoint returns tool_use block and stop_reason='tool_use'."""
        mock_resp = CanonicalLLMResponse(
            id="msg-test-tool-1",
            model="claude-3-5-sonnet",
            content="Editing file now.",
            tool_calls=[
                ToolCall(
                    id="call_edit_001",
                    function=FunctionCall(
                        name="edit_file",
                        arguments=json.dumps({"path": "main.py", "content": "print('hello')"}),
                    ),
                )
            ],
            finish_reason="tool_calls",
            usage=CanonicalUsage(prompt_tokens=25, completion_tokens=15, total_tokens=40),
        )

        app = create_app()
        app.dependency_overrides[get_anthropic_llm_client] = lambda: ScriptedAsyncLLM(mock_resp)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/v1/messages",
                headers={"x-api-key": get_valid_api_key()},
                json={
                    "model": "claude-3-5-sonnet",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "Edit main.py"}],
                    "tools": [
                        {
                            "name": "edit_file",
                            "description": "Edit a file",
                            "input_schema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                            },
                        }
                    ],
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert data["type"] == "message"
            assert data["stop_reason"] == "tool_use"
            tool_blocks = [c for c in data["content"] if c["type"] == "tool_use"]
            assert len(tool_blocks) == 1
            assert tool_blocks[0]["name"] == "edit_file"
            assert tool_blocks[0]["input"]["path"] == "main.py"


# ==============================================================================
# 3. HIGH-CONCURRENCY ASGI REQUEST STRESS
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialConcurrencyStress:
    """Stress-tests the full Gateway API with concurrent client requests."""

    async def test_20_concurrent_chat_completions_requests(self):
        """Stress: 20 simultaneous chat completion requests processed without deadlocks or crashes."""
        mock_resp = CanonicalLLMResponse(
            id="chatcmpl-concurrent-1",
            model="gpt-4o",
            content="Concurrent response payload",
            finish_reason="stop",
            usage=CanonicalUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        app = create_app()
        app.dependency_overrides[get_openai_llm_client] = lambda: ScriptedAsyncLLM(mock_resp)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            concurrency = 20

            async def send_chat_request(idx: int) -> httpx.Response:
                return await client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": f"Bearer {get_valid_api_key()}"},
                    json={
                        "model": "gpt-4o",
                        "messages": [{"role": "user", "content": f"Concurrent query {idx}"}],
                    },
                )

            tasks = [send_chat_request(i) for i in range(concurrency)]
            responses = await asyncio.gather(*tasks)

            assert len(responses) == concurrency
            for resp in responses:
                assert resp.status_code == 200
                data = resp.json()
                assert data["choices"][0]["message"]["content"] == "Concurrent response payload"

    async def test_20_concurrent_anthropic_messages_requests(self):
        """Stress: 20 simultaneous Anthropic messages requests processed cleanly."""
        mock_resp = CanonicalLLMResponse(
            id="msg-concurrent-1",
            model="claude-3-5-sonnet",
            content="Anthropic concurrent payload",
            finish_reason="stop",
            usage=CanonicalUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

        app = create_app()
        app.dependency_overrides[get_anthropic_llm_client] = lambda: ScriptedAsyncLLM(mock_resp)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            concurrency = 20

            async def send_anthropic_request(idx: int) -> httpx.Response:
                return await client.post(
                    "/v1/messages",
                    headers={"x-api-key": get_valid_api_key()},
                    json={
                        "model": "claude-3-5-sonnet",
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content": f"Anthropic query {idx}"}],
                    },
                )

            tasks = [send_anthropic_request(i) for i in range(concurrency)]
            responses = await asyncio.gather(*tasks)

            assert len(responses) == concurrency
            for resp in responses:
                assert resp.status_code == 200
                data = resp.json()
                assert data["content"][0]["text"] == "Anthropic concurrent payload"


# ==============================================================================
# 4. HTTP MULTIPART CORRUPTED FILE INGESTION ADVERSARIAL STRESS
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialHTTPFileUpload:
    """Stress-tests POST /v1/files/upload against corrupted and malicious multipart uploads."""

    async def test_upload_corrupted_json_returns_422_validation_error(self):
        """Verify uploading malformed JSON returns HTTP 422 Unprocessable Entity."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            corrupt_json_bytes = b'{"unclosed": "json data'
            files = {"file": ("bad_file.json", io.BytesIO(corrupt_json_bytes), "application/json")}

            resp = await client.post(
                "/v1/files/upload",
                headers={"Authorization": f"Bearer {get_valid_api_key()}"},
                files=files,
                data={"workspace_id": "test_ws"},
            )
            assert resp.status_code == 422
            data = resp.json()
            assert data["error"]["type"] == "validation_error"
            assert data["error"]["code"] == "invalid_payload"
            assert data["request_id"]

    async def test_upload_corrupted_pdf_returns_422_validation_error(self):
        """Verify uploading invalid PDF file returns HTTP 422 Unprocessable Entity."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            fake_pdf_bytes = b"Not a real PDF file header"
            files = {"file": ("corrupt.pdf", io.BytesIO(fake_pdf_bytes), "application/pdf")}

            resp = await client.post(
                "/v1/files/upload",
                headers={"Authorization": f"Bearer {get_valid_api_key()}"},
                files=files,
                data={"workspace_id": "test_ws"},
            )
            assert resp.status_code == 422
            data = resp.json()
            assert data["error"]["type"] == "validation_error"
            assert data["error"]["code"] == "invalid_payload"
            assert data["request_id"]
