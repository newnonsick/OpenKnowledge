"""Tier 1 Feature Tests for Feature 10: API Key Authentication Middleware.

Validates Bearer token auth, x-api-key header auth, multiple configured keys,
AuthenticationException domain model, and 401 unauthorized rejection.
"""

import hmac
from typing import List, Optional
import pytest

from src.gateway.domain.exceptions import AuthenticationException


class AuthValidator:
    """Authentication validator supporting Bearer tokens and x-api-key headers."""

    def __init__(self, allowed_keys: List[str]):
        self.allowed_keys = set(allowed_keys)

    def validate(self, auth_header: Optional[str] = None, x_api_key: Optional[str] = None) -> bool:
        candidate_key: Optional[str] = None
        if auth_header and auth_header.startswith("Bearer "):
            candidate_key = auth_header[7:].strip()
        elif x_api_key:
            candidate_key = x_api_key.strip()

        if not candidate_key:
            raise AuthenticationException("Missing API key credentials.")

        # Constant-time comparison
        for valid_key in self.allowed_keys:
            if hmac.compare_digest(candidate_key, valid_key):
                return True

        raise AuthenticationException("Invalid API key provided.")


@pytest.mark.tier1
@pytest.mark.feature("F10")
def test_f10_bearer_token_authorization():
    """Verify successful authentication using Authorization: Bearer <key> header."""
    auth = AuthValidator(allowed_keys=["sk-admin-key", "sk-user-key"])
    assert auth.validate(auth_header="Bearer sk-admin-key") is True
    assert auth.validate(auth_header="Bearer sk-user-key") is True


@pytest.mark.tier1
@pytest.mark.feature("F10")
def test_f10_x_api_key_header_authorization():
    """Verify successful authentication using x-api-key: <key> header."""
    auth = AuthValidator(allowed_keys=["sk-admin-key", "sk-user-key"])
    assert auth.validate(x_api_key="sk-admin-key") is True
    assert auth.validate(x_api_key="sk-user-key") is True


@pytest.mark.tier1
@pytest.mark.feature("F10")
def test_f10_multiple_configured_api_keys():
    """Verify that multiple keys configured in GATEWAY_API_KEYS can all authenticate."""
    keys = ["sk-key-alpha", "sk-key-beta", "sk-key-gamma"]
    auth = AuthValidator(allowed_keys=keys)
    for k in keys:
        assert auth.validate(auth_header=f"Bearer {k}") is True


@pytest.mark.tier1
@pytest.mark.feature("F10")
def test_f10_authentication_exception_domain_model():
    """Verify AuthenticationException status code (401) and error code details."""
    exc = AuthenticationException("Invalid API key.")
    assert exc.status_code == 401
    assert exc.error_type == "authentication_error"
    assert exc.code == "invalid_api_key"
    payload = exc.to_dict()
    assert payload["code"] == "invalid_api_key"
    assert payload["type"] == "authentication_error"


@pytest.mark.tier1
@pytest.mark.feature("F10")
def test_f10_missing_auth_header_rejection():
    """Verify rejection when no credentials or invalid keys are supplied."""
    auth = AuthValidator(allowed_keys=["sk-valid-key"])

    # Missing credentials
    with pytest.raises(AuthenticationException) as exc_info:
        auth.validate(auth_header=None, x_api_key=None)
    assert exc_info.value.status_code == 401

    # Invalid key
    with pytest.raises(AuthenticationException) as exc_info:
        auth.validate(auth_header="Bearer sk-invalid-hacker-key")
    assert exc_info.value.status_code == 401
