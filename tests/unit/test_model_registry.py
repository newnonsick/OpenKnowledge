"""Comprehensive unit tests for ModelRegistryService."""

import pytest

from src.gateway.application.services.model_registry import ModelRegistryService
from src.gateway.domain.exceptions import ModelNotFoundException


def test_default_model_from_settings():
    """Verify the registry serves the single default model from settings."""
    registry = ModelRegistryService(default_model="test-default-model")

    assert registry.default_model == "test-default-model"
    assert registry.aliases == {"default": "test-default-model"}


def test_default_alias_resolution():
    """Verify the 'default' alias resolves to the configured default model."""
    registry = ModelRegistryService(default_model="test-default-model")

    assert registry.resolve("default") == "test-default-model"
    assert registry.resolve("DEFAULT") == "test-default-model"


def test_fallback_on_empty_and_whitespace():
    """Verify None, empty string, and whitespace resolve to default model."""
    registry = ModelRegistryService(default_model="fallback-backend-model")

    assert registry.resolve(None) == "fallback-backend-model"
    assert registry.resolve("") == "fallback-backend-model"
    assert registry.resolve("   ") == "fallback-backend-model"


def test_unknown_models_fall_back_to_default():
    """Verify the gateway is single-model: unknown names resolve to the default."""
    registry = ModelRegistryService(default_model="test-default-model")

    assert registry.resolve("coding") == "test-default-model"
    assert registry.resolve("reasoning") == "test-default-model"
    assert registry.resolve("fast") == "test-default-model"
    assert registry.resolve("chat") == "test-default-model"
    assert registry.resolve("mistralai/Mistral-7B-Instruct-v0.3") == "test-default-model"


def test_dynamic_alias_registration():
    """Verify adding dynamic aliases at runtime."""
    registry = ModelRegistryService(default_model="test-default-model")
    registry.register_alias("sql_expert", "defog/sqlcoder-7b-2")

    assert registry.resolve("sql_expert") == "defog/sqlcoder-7b-2"
    assert registry.resolve("SQL_EXPERT") == "defog/sqlcoder-7b-2"


def test_dynamic_model_registration():
    """Verify registering new models and inspecting metadata."""
    registry = ModelRegistryService(default_model="test-default-model")
    registry.register_model(
        model_id="custom-corp-llm",
        context_window=65536,
        owned_by="corp",
        description="Internal enterprise LLM",
    )

    info = registry.get_model_info("custom-corp-llm")
    assert info["id"] == "custom-corp-llm"
    assert info["context_window"] == 65536
    assert info["owned_by"] == "corp"


def test_list_models_structure():
    """Verify list_models returns OpenAI-compatible list of dictionaries."""
    registry = ModelRegistryService(default_model="test-default-model")
    models = registry.list_models()

    model_ids = {m["id"] for m in models}
    assert "test-default-model" in model_ids
    assert "default" in model_ids
    assert "coding" not in model_ids
    assert "reasoning" not in model_ids
    assert "fast" not in model_ids

    for m in models:
        assert m["object"] == "model"
        assert "created" in m
        assert "owned_by" in m


def test_get_model_info_alias_and_root():
    """Verify get_model_info resolves alias and reports root target."""
    registry = ModelRegistryService(default_model="test-default-model")
    info = registry.get_model_info("default")

    assert info["id"] == "default"
    assert info["object"] == "model"
    assert info["root"] == "test-default-model"
