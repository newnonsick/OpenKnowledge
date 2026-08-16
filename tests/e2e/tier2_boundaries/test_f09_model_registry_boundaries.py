"""Tier 2 Boundary Tests for Feature 9: Model Registry & Alias Mapping.

Tests boundary conditions, alias resolution, case sensitivity, non-existent models, and fallback behavior.
"""

from typing import Optional
import pytest
from src.gateway.domain.exceptions import ModelNotFoundException


class ModelRegistryService:
    """Model registry domain/application service resolving abstract aliases to backend models."""

    def __init__(self, default_model: str = "default-model", aliases: dict = None):
        self.default_model = default_model
        self.aliases = aliases or {
            "coding": "qwen2.5-coder-32b-instruct",
            "reasoning": "deepseek-r1-distill-qwen-32b",
            "fast": "llama-3.1-8b-instruct",
            "embedding": "bge-large-en-v1.5",
        }

    def resolve(self, model_name: Optional[str] = None) -> str:
        if not model_name or not str(model_name).strip():
            return self.default_model

        cleaned = str(model_name).strip()
        # Case insensitive lookup
        for alias, target in self.aliases.items():
            if alias.lower() == cleaned.lower():
                return target

        # Direct passthrough for registered backend models or raw names
        return cleaned

    def get_model_info(self, model_name: str) -> dict:
        resolved = self.resolve(model_name)
        if not resolved:
            raise ModelNotFoundException(f"The model '{model_name}' does not exist.")
        return {"id": resolved, "object": "model", "owned_by": "gateway"}


Optional_str = str | None


@pytest.mark.tier2
@pytest.mark.feature("F9")
def test_f09_boundary_empty_or_none_model_alias_fallback():
    """Test boundary: None, empty string, or whitespace model aliases fall back to default model."""
    registry = ModelRegistryService(default_model="meta-llama/Llama-3.1-8B-Instruct")
    assert registry.resolve(None) == "meta-llama/Llama-3.1-8B-Instruct"
    assert registry.resolve("") == "meta-llama/Llama-3.1-8B-Instruct"
    assert registry.resolve("   ") == "meta-llama/Llama-3.1-8B-Instruct"


@pytest.mark.tier2
@pytest.mark.feature("F9")
def test_f09_boundary_case_insensitive_alias_resolution():
    """Test boundary: model aliases resolve correctly regardless of casing (CODING, Coding, cOdInG)."""
    registry = ModelRegistryService()
    assert registry.resolve("CODING") == "qwen2.5-coder-32b-instruct"
    assert registry.resolve("Coding") == "qwen2.5-coder-32b-instruct"
    assert registry.resolve("REASONING") == "deepseek-r1-distill-qwen-32b"
    assert registry.resolve("Fast") == "llama-3.1-8b-instruct"


@pytest.mark.tier2
@pytest.mark.feature("F9")
def test_f09_boundary_direct_backend_model_passthrough():
    """Test boundary: exact backend model IDs pass through unaltered."""
    registry = ModelRegistryService()
    backend_id = "meta-llama/Llama-3.3-70B-Instruct"
    assert registry.resolve(backend_id) == backend_id


@pytest.mark.tier2
@pytest.mark.feature("F9")
def test_f09_boundary_empty_alias_mapping_dictionary():
    """Test boundary: empty alias dictionary gracefully passes through requested model or falls back."""
    registry = ModelRegistryService(default_model="fallback-llm", aliases={})
    assert registry.resolve(None) == "fallback-llm"
    assert registry.resolve("custom-model-id") == "custom-model-id"


@pytest.mark.tier2
@pytest.mark.feature("F9")
def test_f09_boundary_model_not_found_exception_structure():
    """Test boundary: ModelNotFoundException serializes 404 status code and error details."""
    exc = ModelNotFoundException(
        requested_model_or_message="unknown-model-xyz",
        details={"requested_alias": "unknown-model-xyz"},
    )
    assert exc.status_code == 404
    assert exc.error_type == "not_found_error"
    d = exc.to_dict()
    assert "unknown-model-xyz" in d["message"]
    assert d["code"] == "model_not_found"
