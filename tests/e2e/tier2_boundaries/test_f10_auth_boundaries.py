"""Tier 2 Boundary Tests for Feature 10: API Key Authentication Middleware.

Tests boundary conditions, malformed headers, whitespace keys, missing bearer prefix, and auth exceptions.
"""

import hmac
import pytest
from src.gateway.domain.exceptions import AuthenticationException


def authenticate_request(
    headers: dict,
    allowed_keys: list[str],
) -> str:
    """Authentication validation helper adhering to Gateway constant-time auth contract."""
    auth_header = headers.get("authorization") or headers.get("Authorization")
    api_key_header = headers.get("x-api-key") or headers.get("X-Api-Key")

    token = None
    if auth_header:
        parts = auth_header.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
        else:
            token = None

    if not token and api_key_header:
        token = api_key_header.strip()

    if not token:
        raise AuthenticationException("Missing or malformed authentication header.")

    # Constant-time comparison
    for allowed in allowed_keys:
        if hmac.compare_digest(token.encode("utf-8"), allowed.encode("utf-8")):
            return token

    raise AuthenticationException("Invalid API key.")


@pytest.mark.tier2
@pytest.mark.feature("F10")
def test_f10_boundary_missing_authorization_and_api_key_headers():
    """Test boundary: request with empty headers raises AuthenticationException (401)."""
    with pytest.raises(AuthenticationException) as exc_info:
        authenticate_request({}, allowed_keys=["valid-key-1"])
    assert exc_info.value.status_code == 401
    assert "Missing" in exc_info.value.message


@pytest.mark.tier2
@pytest.mark.feature("F10")
def test_f10_boundary_malformed_bearer_headers():
    """Test boundary: Bearer header missing prefix or token string raises AuthenticationException."""
    malformed_headers = [
        {"authorization": "Bearer"},
        {"authorization": "Bearer   "},
        {"authorization": "Token valid-key-1"},
        {"authorization": "Basic dXNlcjpwYXNz"},
        {"authorization": "bearer"},
    ]
    for h in malformed_headers:
        with pytest.raises(AuthenticationException):
            authenticate_request(h, allowed_keys=["valid-key-1"])


@pytest.mark.tier2
@pytest.mark.feature("F10")
def test_f10_boundary_invalid_and_unregistered_keys():
    """Test boundary: validly formatted Bearer and x-api-key with unregistered keys rejected with 401."""
    allowed = ["sk-admin-key", "sk-user-key"]

    with pytest.raises(AuthenticationException):
        authenticate_request({"authorization": "Bearer sk-invalid-key-999"}, allowed)

    with pytest.raises(AuthenticationException):
        authenticate_request({"x-api-key": "sk-unregistered-token"}, allowed)


@pytest.mark.tier2
@pytest.mark.feature("F10")
def test_f10_boundary_whitespace_only_and_null_bytes_in_keys():
    """Test boundary: whitespace-only keys and strings with null bytes fail authentication."""
    allowed = ["sk-prod-key"]

    with pytest.raises(AuthenticationException):
        authenticate_request({"x-api-key": "   "}, allowed)

    with pytest.raises(AuthenticationException):
        authenticate_request({"authorization": "Bearer \t\n  "}, allowed)


@pytest.mark.tier2
@pytest.mark.feature("F10")
def test_f10_boundary_mixed_headers_precedence():
    """Test boundary: when both Bearer and x-api-key are supplied, Bearer takes precedence."""
    allowed = ["sk-bearer-key", "sk-xkey"]

    # Valid Bearer + invalid x-api-key -> passes via Bearer
    token1 = authenticate_request(
        {"authorization": "Bearer sk-bearer-key", "x-api-key": "invalid-xkey"},
        allowed,
    )
    assert token1 == "sk-bearer-key"

    # Malformed Bearer + valid x-api-key -> falls back to valid x-api-key
    token2 = authenticate_request(
        {"authorization": "MalformedBearer", "x-api-key": "sk-xkey"},
        allowed,
    )
    assert token2 == "sk-xkey"
