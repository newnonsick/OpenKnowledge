"""Deep dive tests on configuration edge cases and boundary conditions."""

import pytest
from pydantic import ValidationError
from src.gateway.config import (
    AppSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GatewaySettings,
    LLMSettings,
    Settings,
)


def test_config_negative_and_zero_values():
    """Verify how settings reject negative or zero values with ValidationError."""
    # Negative context window
    with pytest.raises(ValidationError):
        LLMSettings(context_window=-100, temperature=-2.0, timeout_seconds=-5.0)

    # Negative embedding dimension
    with pytest.raises(ValidationError):
        EmbeddingSettings(dimension=-768, batch_size=0, timeout_seconds=-1.0)

    # Negative DB pool size
    with pytest.raises(ValidationError):
        DatabaseSettings(pool_size=-10, max_overflow=-5, pool_timeout=-30.0)

    # Negative tool iterations
    with pytest.raises(ValidationError):
        GatewaySettings(max_tool_iterations=-1, tool_timeout_seconds=-10.0)


def test_config_json_api_keys_edge_cases():
    """Test various JSON structures for api_keys."""
    # JSON containing numbers and booleans
    gw1 = GatewaySettings(api_keys='[123, true, "valid_key"]')
    assert gw1.api_keys == ["123", "True", "valid_key"]

    # JSON empty array
    gw2 = GatewaySettings(api_keys="[]")
    assert gw2.api_keys == []

    # JSON with nulls
    gw3 = GatewaySettings(api_keys='["key1", null, "key2"]')
    assert gw3.api_keys == ["key1", "None", "key2"]


def test_config_case_insensitivity_and_aliases(monkeypatch):
    """Test alias precedence and case matching in AppSettings."""
    monkeypatch.setenv("llm_url", "http://lowercase-llm:8000")
    monkeypatch.setenv("LLM_URL", "http://uppercase-llm:8000")

    # In Pydantic Settings, environment variables match aliases
    s = Settings()
    assert s.llm.url in ("http://lowercase-llm:8000", "http://uppercase-llm:8000")
