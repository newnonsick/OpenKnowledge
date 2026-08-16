"""Unit tests for Pydantic Settings and environment configuration."""

import pytest
from pydantic import ValidationError

from src.gateway.config import (
    DatabaseSettings,
    EmbeddingSettings,
    GatewaySettings,
    LLMSettings,
    Settings,
    get_settings,
)


def test_default_settings(tmp_path, monkeypatch):
    """Test default values across all settings sub-models, isolated from any .env file."""
    for var in (
        "LLM_URL", "LLM_MODEL_ID", "CONTEXT_WINDOW",
        "EMBEDDING_URL", "EMBEDDING_DIMENSION",
        "DATABASE_URL", "STORAGE_DIR", "LOG_LEVEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here -> pure pydantic defaults

    settings = Settings()

    assert settings.llm.url == "http://localhost:8888"
    assert settings.llm.model_id == "default"
    assert settings.llm.context_window == 8192

    assert settings.embedding.url == "http://localhost:7997"
    assert settings.embedding.model_id == "default"
    assert settings.embedding.dimension == 768

    assert "postgresql+asyncpg" in settings.database.url
    assert settings.gateway.storage_dir == "./data/storage"
    assert settings.gateway.log_level == "DEBUG"
    assert settings.gateway.max_tool_iterations == 10


def test_env_file_overrides_defaults(tmp_path, monkeypatch):
    """Verify values from a .env file in the working directory win over pydantic defaults."""
    for var in ("LLM_MODEL_ID", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / ".env").write_text(
        "LLM_MODEL_ID=env-file-model\n"
        "DATABASE_URL=postgresql://envuser:envpass@envhost:5432/envdb\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.llm.model_id == "env-file-model"
    assert settings.database.url == "postgresql://envuser:envpass@envhost:5432/envdb"


def test_environment_variable_overrides(monkeypatch):
    """Test loading configuration from environment variables."""
    monkeypatch.setenv("LLM_URL", "http://custom-llm:8000")
    monkeypatch.setenv("LLM_MODEL_ID", "llama-3-70b")
    monkeypatch.setenv("CONTEXT_WINDOW", "32768")
    monkeypatch.setenv("EMBEDDING_URL", "http://custom-embed:7000")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "1024")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@db:5432/prod_db")
    monkeypatch.setenv("GATEWAY_API_KEYS", "key1,key2,key3")
    monkeypatch.setenv("STORAGE_DIR", "/var/data/gateway")

    settings = Settings()
    assert settings.llm.url == "http://custom-llm:8000"
    assert settings.llm.model_id == "llama-3-70b"
    assert settings.llm.context_window == 32768
    assert settings.embedding.url == "http://custom-embed:7000"
    assert settings.embedding.dimension == 1024
    assert settings.database.url == "postgresql+asyncpg://user:pass@db:5432/prod_db"
    assert settings.gateway.api_keys == ["key1", "key2", "key3"]
    assert settings.gateway.storage_dir == "/var/data/gateway"


def test_decoupled_llm_and_embedding_settings():
    """Verify that LLM and Embedding settings are completely independent."""
    settings = Settings(
        llm={"url": "http://llm-host:8888", "api_key": "llm-key"},
        embedding={"url": "http://embed-host:7997", "api_key": "embed-key"},
    )
    assert settings.llm.url != settings.embedding.url
    assert settings.llm.api_key != settings.embedding.api_key


def test_api_keys_comma_separation():
    """Verify parsing comma-separated API keys into list."""
    gateway_conf = GatewaySettings(api_keys="key_a, key_b , key_c")
    assert gateway_conf.api_keys == ["key_a", "key_b", "key_c"]
    assert gateway_conf.gateway_api_keys == ["key_a", "key_b", "key_c"]

    # Test JSON string parsing
    gw_json = GatewaySettings(api_keys='["json_k1", "json_k2"]')
    assert gw_json.api_keys == ["json_k1", "json_k2"]

    # Test list input
    gw_list = GatewaySettings(api_keys=["k1", "k2"])
    assert gw_list.api_keys == ["k1", "k2"]


def test_cors_origins_parsing():
    """Verify parsing CORS origins."""
    gw_cors = GatewaySettings(cors_origins="http://localhost:3000, https://app.example.com")
    assert gw_cors.cors_origins == ["http://localhost:3000", "https://app.example.com"]

    gw_cors_json = GatewaySettings(cors_origins='["http://localhost:3000"]')
    assert gw_cors_json.cors_origins == ["http://localhost:3000"]


def test_invalid_type_validation_error(monkeypatch):
    """Verify ValidationError on invalid type parsing."""
    monkeypatch.setenv("CONTEXT_WINDOW", "not_a_number")
    with pytest.raises(ValidationError):
        LLMSettings()


def test_get_settings_cached():
    """Verify get_settings returns cached AppSettings singleton."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
