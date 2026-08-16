"""Tier 1 Feature Tests for Feature 9: Model Registry & Alias Mapping.

Validates model alias mapping, /v1/models endpoint discovery, alias resolution,
and model metadata inspection against the REAL ModelRegistryService and the
real /v1/models gateway endpoints.
"""

import pytest

from src.gateway.application.services.model_registry import ModelRegistryService
from src.gateway.domain.exceptions import ModelNotFoundException
from tests.e2e.harness.test_env import GatewayMockUpstream


@pytest.fixture
def registry() -> ModelRegistryService:
    """Real registry instance with the canonical documented aliases registered."""
    reg = ModelRegistryService(default_model="meta-llama/Llama-3.1-8B-Instruct")
    reg.register_alias("coding", "meta-llama/Llama-3.1-8B-Instruct")
    reg.register_alias("reasoning", "deepseek-ai/DeepSeek-R1-Distill-Llama-8B")
    reg.register_alias("fast", "Qwen/Qwen2.5-Coder-7B-Instruct")
    reg.register_model(
        "deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
        context_window=16384,
        owned_by="deepseek",
    )
    reg.register_model(
        "Qwen/Qwen2.5-Coder-7B-Instruct",
        context_window=32768,
        owned_by="qwen",
    )
    return reg


@pytest.mark.tier1
@pytest.mark.feature("F9")
def test_f09_model_registry_alias_mapping(registry: ModelRegistryService):
    """Verify mapping of abstract model aliases (coding, reasoning, fast) to backend models."""
    assert registry.resolve("coding") == "meta-llama/Llama-3.1-8B-Instruct"
    assert registry.resolve("reasoning") == "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"
    assert registry.resolve("fast") == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert registry.resolve("default") == "meta-llama/Llama-3.1-8B-Instruct"


@pytest.mark.tier1
@pytest.mark.feature("F9")
@pytest.mark.asyncio
async def test_f09_models_endpoint_discovery():
    """Verify the real /v1/models endpoint lists backend models and aliases."""
    async with GatewayMockUpstream() as gw:
        resp = await gw.client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "list"
        model_ids = {m["id"] for m in data["data"]}
        assert "mock-llama-3.1-8b" in model_ids  # TestEnvironment LLM_MODEL_ID
        assert "default" in model_ids  # built-in alias
        for m in data["data"]:
            assert m["object"] == "model"
            assert "owned_by" in m


@pytest.mark.tier1
@pytest.mark.feature("F9")
@pytest.mark.asyncio
async def test_f09_model_resolution_in_chat_request():
    """Verify an alias requested via /v1/chat/completions resolves to the backend model
    before being forwarded upstream."""
    async with GatewayMockUpstream() as gw:
        gw.llm.queue_text_response("Resolved model generated response.")

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 200
        assert gw.llm.last_request is not None
        # The gateway resolves the alias to the configured backend model id
        assert gw.llm.last_request.model == "mock-llama-3.1-8b"
        assert "Resolved model generated response." in resp.json()["choices"][0]["message"]["content"]


@pytest.mark.tier1
@pytest.mark.feature("F9")
def test_f09_unknown_model_falls_back_to_default(registry: ModelRegistryService):
    """Verify single-model gateway fallback: unknown names resolve to the default
    model instead of failing the request (documented registry contract)."""
    assert registry.resolve("non_existent_model_v99") == "meta-llama/Llama-3.1-8B-Instruct"
    assert registry.resolve("") == "meta-llama/Llama-3.1-8B-Instruct"
    assert registry.resolve(None) == "meta-llama/Llama-3.1-8B-Instruct"


@pytest.mark.tier1
@pytest.mark.feature("F9")
def test_f09_model_not_found_exception_shape():
    """Verify ModelNotFoundException carries the 404 not-found error contract."""
    exc = ModelNotFoundException("non_existent_model_v99")
    assert exc.status_code == 404
    assert exc.code == "model_not_found"
    assert "non_existent_model_v99" in exc.message


@pytest.mark.tier1
@pytest.mark.feature("F9")
def test_f09_model_metadata_inspection(registry: ModelRegistryService):
    """Verify model metadata attributes (context window, owned_by) via get_model_info.

    get_model_info describes the RESOLVED backend model (alias lookup returns
    the target's metadata plus a ``root`` pointer to the alias target).
    """
    info = registry.get_model_info("deepseek-ai/DeepSeek-R1-Distill-Llama-8B")
    assert info["context_window"] == 16384
    assert info["owned_by"] == "deepseek"

    fast_info = registry.get_model_info("fast")
    assert fast_info["root"] == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert fast_info["owned_by"] == "qwen"  # metadata of the resolved backend model
    assert fast_info["context_window"] == 32768


@pytest.mark.tier1
@pytest.mark.feature("F9")
@pytest.mark.asyncio
async def test_f09_get_model_endpoint_metadata():
    """Verify the real /v1/models/{id} endpoint returns model metadata."""
    async with GatewayMockUpstream() as gw:
        resp = await gw.client.get("/v1/models/default")
        assert resp.status_code == 200
        data = resp.json()
        assert data["object"] == "model"
        assert data["root"] == "mock-llama-3.1-8b"  # alias resolved to backend id
