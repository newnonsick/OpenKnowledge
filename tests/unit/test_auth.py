"""Comprehensive unit tests for API Key Authentication Middleware and helpers."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from fastapi.testclient import TestClient
import pytest

from src.gateway.domain.exceptions import AuthenticationException
from src.gateway.domain.identity import PrincipalKind
from src.gateway.infrastructure.persistence.principal_context import get_bound_principal
from src.gateway.presentation.auth import (
    APIKeyAuthMiddleware,
    AuthValidator,
    authenticate_credentials,
    authenticate_request,
    is_public_path,
)


def test_bearer_token_success():
    """Verify Bearer token authentication with exact and case-insensitive prefix."""
    allowed = ["sk-secret-key-1", "sk-secret-key-2"]

    # Standard Bearer
    token = authenticate_credentials(
        auth_header="Bearer sk-secret-key-1",
        allowed_keys=allowed,
    )
    assert token == "sk-secret-key-1"

    # Lowercase bearer
    token_lower = authenticate_credentials(
        auth_header="bearer sk-secret-key-2",
        allowed_keys=allowed,
    )
    assert token_lower == "sk-secret-key-2"


def test_x_api_key_header_success():
    """Verify x-api-key authentication."""
    allowed = ["sk-api-key-prod"]

    token = authenticate_credentials(
        x_api_key="sk-api-key-prod",
        allowed_keys=allowed,
    )
    assert token == "sk-api-key-prod"


def test_headers_dictionary_authentication():
    """Verify authenticate_request helper with headers dictionary."""
    allowed = ["sk-token-alpha", "sk-token-beta"]

    # Bearer header
    headers1 = {"Authorization": "Bearer sk-token-alpha"}
    assert authenticate_request(headers1, allowed_keys=allowed) == "sk-token-alpha"

    # x-api-key header
    headers2 = {"x-api-key": "sk-token-beta"}
    assert authenticate_request(headers2, allowed_keys=allowed) == "sk-token-beta"

    # Precedence: valid Bearer takes precedence
    headers3 = {"Authorization": "Bearer sk-token-alpha", "x-api-key": "invalid-key"}
    assert authenticate_request(headers3, allowed_keys=allowed) == "sk-token-alpha"


def test_missing_credentials_raises_auth_exception():
    """Verify missing credentials raise AuthenticationException with 401."""
    with pytest.raises(AuthenticationException) as exc_info:
        authenticate_credentials(auth_header=None, x_api_key=None, allowed_keys=["sk-valid"])

    assert exc_info.value.status_code == 401
    assert "Missing" in exc_info.value.message


def test_invalid_key_raises_auth_exception():
    """Verify invalid key raises AuthenticationException with 401."""
    with pytest.raises(AuthenticationException) as exc_info:
        authenticate_credentials(
            auth_header="Bearer sk-invalid-attacker-key",
            allowed_keys=["sk-valid-key"],
        )

    assert exc_info.value.status_code == 401
    assert "Invalid" in exc_info.value.message


def test_whitespace_and_null_bytes_rejection():
    """Verify whitespace-only keys and null bytes are rejected."""
    allowed = ["sk-valid-key"]

    with pytest.raises(AuthenticationException):
        authenticate_credentials(auth_header="Bearer    ", allowed_keys=allowed)

    with pytest.raises(AuthenticationException):
        authenticate_credentials(x_api_key="   ", allowed_keys=allowed)

    with pytest.raises(AuthenticationException):
        authenticate_credentials(auth_header="Bearer sk\x00key", allowed_keys=allowed)


def test_auth_validator_class():
    """Verify AuthValidator class helper."""
    validator = AuthValidator(allowed_keys=["sk-key-1", "sk-key-2"])
    assert validator.validate(auth_header="Bearer sk-key-1") is True
    assert validator.validate(x_api_key="sk-key-2") is True

    with pytest.raises(AuthenticationException):
        validator.validate(auth_header="Bearer sk-wrong")


def test_public_path_bypass_detection():
    """Verify public path bypass logic."""
    assert is_public_path("/health") is True
    assert is_public_path("/docs") is True
    assert is_public_path("/openapi.json") is True
    assert is_public_path("/redoc") is True
    assert is_public_path("/v1/chat/completions") is False
    assert is_public_path("/v1/messages") is False
    assert is_public_path("/v1/models") is False


def test_api_key_auth_middleware_with_fastapi():
    """Verify APIKeyAuthMiddleware behavior in a FastAPI test app."""
    app = FastAPI()
    app.add_middleware(APIKeyAuthMiddleware, allowed_keys=["sk-test-secret"])

    @app.get("/health")
    def health_endpoint():
        return {"status": "healthy"}

    @app.post("/v1/chat/completions")
    def protected_chat():
        return {"result": "chat_ok"}

    @app.post("/v1/messages")
    def protected_messages():
        return {"result": "messages_ok"}

    client = TestClient(app)

    # 1. Public endpoint bypasses auth
    resp_health = client.get("/health")
    assert resp_health.status_code == 200

    # 2. Protected OpenAI endpoint without auth returns 401 OpenAI format
    resp_no_auth = client.post("/v1/chat/completions")
    assert resp_no_auth.status_code == 401
    assert "error" in resp_no_auth.json()
    assert resp_no_auth.json()["error"]["code"] == "invalid_api_key"

    # 3. Protected Anthropic endpoint without auth returns 401 Anthropic format
    resp_anthropic_no_auth = client.post("/v1/messages")
    assert resp_anthropic_no_auth.status_code == 401
    anthropic_err = resp_anthropic_no_auth.json()
    assert anthropic_err.get("type") == "error"
    assert anthropic_err["error"]["type"] == "authentication_error"

    # 4. Protected endpoint with valid Bearer auth succeeds
    resp_valid_bearer = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test-secret"},
    )
    assert resp_valid_bearer.status_code == 200
    assert resp_valid_bearer.json()["result"] == "chat_ok"

    # 5. Protected endpoint with valid x-api-key succeeds
    resp_valid_xkey = client.post(
        "/v1/messages",
        headers={"x-api-key": "sk-test-secret"},
    )
    assert resp_valid_xkey.status_code == 200
    assert resp_valid_xkey.json()["result"] == "messages_ok"


def test_legacy_middleware_exposes_only_a_redacted_principal():
    app = FastAPI()
    app.add_middleware(APIKeyAuthMiddleware, allowed_keys=["sk-test-secret"])

    @app.get("/principal")
    def principal_endpoint(request: Request):
        principal = request.state.principal
        return {
            "kind": principal.kind.value,
            "representation": repr(principal),
            "raw_key_attached": hasattr(request.state, "api_key"),
        }

    response = TestClient(app).get(
        "/principal",
        headers={"Authorization": "Bearer sk-test-secret"},
    )

    assert response.status_code == 200
    assert response.json()["kind"] == PrincipalKind.COMPATIBILITY.value
    assert "sk-test-secret" not in response.json()["representation"]
    assert response.json()["raw_key_attached"] is False


def test_legacy_credentials_require_explicit_compatibility_switch():
    app = FastAPI()
    app.add_middleware(
        APIKeyAuthMiddleware,
        allowed_keys=["sk-test-secret"],
        legacy_api_keys_enabled=False,
    )

    @app.get("/protected")
    def protected_endpoint():
        return {"ok": True}

    response = TestClient(app).get(
        "/protected",
        headers={"Authorization": "Bearer sk-test-secret"},
    )

    assert response.status_code == 401


def test_principal_context_remains_bound_during_streaming_body():
    app = FastAPI()
    app.add_middleware(APIKeyAuthMiddleware, allowed_keys=["sk-stream-secret"])

    @app.get("/stream")
    async def stream_endpoint():
        async def body():
            principal = get_bound_principal()
            yield principal.kind.value if principal is not None else "missing"

        return StreamingResponse(body())

    response = TestClient(app).get(
        "/stream",
        headers={"Authorization": "Bearer sk-stream-secret"},
    )

    assert response.status_code == 200
    assert response.text == PrincipalKind.COMPATIBILITY.value
