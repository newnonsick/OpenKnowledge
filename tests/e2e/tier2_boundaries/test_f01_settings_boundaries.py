"""Tier 2 Boundary Tests for Feature 1: Decoupled Pydantic Settings & .env.example.

Tests boundary conditions, limits, malformed inputs, and error handling for configuration.
"""

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


@pytest.mark.tier2
@pytest.mark.feature("F1")
def test_f01_boundary_missing_and_empty_env_vars(monkeypatch, tmp_path):
    """Test boundary: empty or missing environment variables fall back to valid defaults."""
    monkeypatch.delenv("LLM_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here -> defaults apply despite file config

    app_settings = AppSettings()
    assert app_settings.llm.url == "http://localhost:8888"
    assert app_settings.embedding.url == "http://localhost:7997"
    assert "postgresql+asyncpg" in app_settings.database.url
    assert len(app_settings.gateway.api_keys) > 0


@pytest.mark.tier2
@pytest.mark.feature("F1")
def test_f01_boundary_malformed_urls_and_hosts():
    """Test boundary: handling of unusual, non-standard, or edge-case URL and host strings."""
    # URLs with custom ports, IPv6, trailing slashes, or special paths
    llm = LLMSettings(url="http://[::1]:9999/v1/custom/subpath/")
    assert llm.url == "http://[::1]:9999/v1/custom/subpath/"

    emb = EmbeddingSettings(url="https://embed-internal.corp.local:443/embed")
    assert emb.url == "https://embed-internal.corp.local:443/embed"

    # Gateway host with unusual IP
    gw = GatewaySettings(host="127.0.0.254")
    assert gw.host == "127.0.0.254"


@pytest.mark.tier2
@pytest.mark.feature("F1")
def test_f01_boundary_invalid_port_numbers():
    """Test boundary: port number limits (0, negative, > 65535, non-integer) raise ValidationError."""
    # Port 0 (invalid per gt=0)
    with pytest.raises(ValidationError):
        GatewaySettings(port=0)

    # Negative port
    with pytest.raises(ValidationError):
        GatewaySettings(port=-8080)

    # Port exceeding 65535
    with pytest.raises(ValidationError):
        GatewaySettings(port=65536)

    # String non-integer port
    with pytest.raises(ValidationError):
        GatewaySettings(port="not_a_port")

    # Boundary valid ports
    gw_min = GatewaySettings(port=1)
    gw_max = GatewaySettings(port=65535)
    assert gw_min.port == 1
    assert gw_max.port == 65535


@pytest.mark.tier2
@pytest.mark.feature("F1")
def test_f01_boundary_negative_and_zero_dimensions():
    """Test boundary: embedding dimensions, batch sizes, context windows, pool sizes must be positive."""
    # Negative embedding dimension
    with pytest.raises(ValidationError):
        EmbeddingSettings(dimension=0)

    with pytest.raises(ValidationError):
        EmbeddingSettings(dimension=-512)

    # Zero/negative context window
    with pytest.raises(ValidationError):
        LLMSettings(context_window=0)

    with pytest.raises(ValidationError):
        LLMSettings(context_window=-1024)

    # Negative DB pool size
    with pytest.raises(ValidationError):
        DatabaseSettings(pool_size=0)

    with pytest.raises(ValidationError):
        DatabaseSettings(pool_size=-5)

    # Zero/negative max tool iterations
    with pytest.raises(ValidationError):
        GatewaySettings(max_tool_iterations=0)

    with pytest.raises(ValidationError):
        GatewaySettings(max_tool_iterations=-10)


@pytest.mark.tier2
@pytest.mark.feature("F1")
def test_f01_boundary_empty_strings_and_whitespace_keys():
    """Test boundary: parsing empty strings, whitespace, and complex JSON arrays for API keys."""
    # Empty string -> empty list
    gw_empty = GatewaySettings(api_keys="")
    assert gw_empty.api_keys == []

    # Whitespace-only string -> empty list
    gw_spaces = GatewaySettings(api_keys="   ,  \t ,   \n  ")
    assert gw_spaces.api_keys == []

    # Mixed whitespace around valid keys
    gw_mixed = GatewaySettings(api_keys=" key-alpha  ,  key-beta , key-gamma ")
    assert gw_mixed.api_keys == ["key-alpha", "key-beta", "key-gamma"]

    # Malformed JSON fallback
    gw_bad_json = GatewaySettings(api_keys='["key1", "key2"')
    assert len(gw_bad_json.api_keys) > 0
