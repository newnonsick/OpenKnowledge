"""Tier 1 Feature Tests for Feature 1: Decoupled Pydantic Settings & .env.example.

Validates that Pydantic settings are modular, decoupled (LLM vs Embedding), support env overrides,
properly validate inputs, and that .env.example contains all required configuration options.
"""

from pathlib import Path
import pytest
from pydantic import ValidationError

from src.gateway.config import (
    AppSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GatewaySettings,
    LLMSettings,
    Settings,
    get_settings,
)


@pytest.mark.tier1
@pytest.mark.feature("F1")
def test_f01_default_settings_instantiation(tmp_path, monkeypatch):
    """Verify default values across all sub-models (LLM, Embedding, Database, Gateway)."""
    for var in (
        "LLM_URL", "LLM_MODEL_ID", "LLM_API_KEY", "CONTEXT_WINDOW", "LLM_TEMPERATURE",
        "EMBEDDING_URL", "EMBEDDING_MODEL_ID", "EMBEDDING_DIMENSION", "EMBEDDING_BATCH_SIZE",
        "DATABASE_URL", "DB_POOL_SIZE", "HOST", "PORT", "LOG_LEVEL", "STORAGE_DIR",
        "MAX_TOOL_ITERATIONS", "DEFAULT_WORKSPACE_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here -> pure pydantic defaults

    cfg = Settings()

    # LLM Defaults
    assert cfg.llm.url == "http://localhost:8888"
    assert cfg.llm.model_id == "default"
    assert cfg.llm.api_key == "EMPTY"
    assert cfg.llm.context_window == 8192
    assert cfg.llm.temperature == 0.7

    # Embedding Defaults
    assert cfg.embedding.url == "http://localhost:7997"
    assert cfg.embedding.model_id == "default"
    assert cfg.embedding.dimension == 768
    assert cfg.embedding.batch_size == 32

    # Database Defaults
    assert "postgresql+asyncpg" in cfg.database.url
    assert cfg.database.pool_size == 20

    # Gateway Defaults
    assert cfg.gateway.host == "0.0.0.0"
    assert cfg.gateway.port == 8000
    assert cfg.gateway.log_level == "DEBUG"
    assert cfg.gateway.storage_dir == "./data/storage"
    assert cfg.gateway.max_tool_iterations == 10
    assert cfg.gateway.default_workspace_id == "global"


@pytest.mark.tier1
@pytest.mark.feature("F1")
def test_f01_decoupled_llm_and_embedding_configs():
    """Verify LLM and Embedding settings are fully decoupled with distinct properties."""
    cfg = Settings(
        llm={
            "url": "http://vllm-node:8000/v1",
            "model_id": "meta-llama/Llama-3.1-8B-Instruct",
            "api_key": "vllm-secret-key",
            "context_window": 16384,
        },
        embedding={
            "url": "http://tei-node:8080",
            "model_id": "BAAI/bge-large-en-v1.5",
            "api_key": "tei-secret-key",
            "dimension": 1024,
        },
    )

    assert cfg.llm.url != cfg.embedding.url
    assert cfg.llm.model_id != cfg.embedding.model_id
    assert cfg.llm.api_key != cfg.embedding.api_key
    assert cfg.llm.context_window == 16384
    assert cfg.embedding.dimension == 1024


@pytest.mark.tier1
@pytest.mark.feature("F1")
def test_f01_env_var_overrides(monkeypatch):
    """Verify environment variable overrides across all settings components."""
    monkeypatch.setenv("LLM_URL", "http://env-llm:9000")
    monkeypatch.setenv("LLM_MODEL_ID", "env-model-70b")
    monkeypatch.setenv("CONTEXT_WINDOW", "32768")
    monkeypatch.setenv("EMBEDDING_URL", "http://env-embed:9001")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "384")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://usr:pwd@env-db:5432/appdb")
    monkeypatch.setenv("STORAGE_DIR", "/opt/gateway/storage")
    monkeypatch.setenv("MAX_TOOL_ITERATIONS", "15")

    cfg = AppSettings()
    assert cfg.llm.url == "http://env-llm:9000"
    assert cfg.llm.model_id == "env-model-70b"
    assert cfg.llm.context_window == 32768
    assert cfg.embedding.url == "http://env-embed:9001"
    assert cfg.embedding.dimension == 384
    assert cfg.database.url == "postgresql+asyncpg://usr:pwd@env-db:5432/appdb"
    assert cfg.gateway.storage_dir == "/opt/gateway/storage"
    assert cfg.gateway.max_tool_iterations == 15


@pytest.mark.tier1
@pytest.mark.feature("F1")
def test_f01_api_keys_and_cors_parsing():
    """Verify parsing of comma-separated strings, JSON lists, and native lists."""
    # Comma-separated string
    gw1 = GatewaySettings(api_keys="sk-admin-key, sk-user-1, sk-user-2")
    assert gw1.api_keys == ["sk-admin-key", "sk-user-1", "sk-user-2"]
    assert gw1.gateway_api_keys == ["sk-admin-key", "sk-user-1", "sk-user-2"]

    # JSON array string
    gw2 = GatewaySettings(api_keys='["sk-json-1", "sk-json-2"]')
    assert gw2.api_keys == ["sk-json-1", "sk-json-2"]

    # List of strings
    gw3 = GatewaySettings(api_keys=["sk-list-1", "sk-list-2"])
    assert gw3.api_keys == ["sk-list-1", "sk-list-2"]

    # CORS origins
    gw_cors = GatewaySettings(cors_origins="http://localhost:3000, https://editor.app")
    assert gw_cors.cors_origins == ["http://localhost:3000", "https://editor.app"]


@pytest.mark.tier1
@pytest.mark.feature("F1")
def test_f01_env_example_file_completeness():
    """Verify that .env.example exists in project root and documents all required variables."""
    env_example_path = Path(".env.example")
    assert env_example_path.exists(), ".env.example file must exist in repository root"

    content = env_example_path.read_text(encoding="utf-8")
    assert len(content) > 50

    # Verify essential environment variables are documented
    expected_vars = [
        "LLM_URL",
        "LLM_MODEL_ID",
        "LLM_API_KEY",
        "CONTEXT_WINDOW",
        "EMBEDDING_URL",
        "EMBEDDING_MODEL_ID",
        "EMBEDDING_API_KEY",
        "EMBEDDING_DIMENSION",
        "DATABASE_URL",
        "STORAGE_DIR",
        "GATEWAY_API_KEYS",
    ]
    for var in expected_vars:
        assert var in content, f"Expected {var} to be documented in .env.example"
