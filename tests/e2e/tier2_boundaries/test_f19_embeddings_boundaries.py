"""Tier 2 Boundary Tests for Feature 19: HTTP Embedding Client & Vector Indexing.

Tests boundary conditions, vector normalization, zero-length texts, batch sizes, and upstream errors.
"""

import math
import httpx
import pytest

from src.gateway.domain.exceptions import EmbeddingException
from src.gateway.infrastructure.adapters.upstream_resilience import ResiliencePolicy
from tests.e2e.harness.mock_server import DeterministicEmbeddingEngine, MockServerManager


@pytest.mark.tier2
@pytest.mark.feature("F19")
def test_f19_boundary_zero_length_text_vector_generation():
    """Test boundary: embedding engine generates unit-normalized vector for empty string."""
    engine = DeterministicEmbeddingEngine(dimension=384)
    vec = engine.generate_vector("")
    assert len(vec) == 384
    norm = math.sqrt(sum(x * x for x in vec))
    assert abs(norm - 1.0) < 1e-4


@pytest.mark.tier2
@pytest.mark.feature("F19")
def test_f19_boundary_custom_registered_vector_dimension_mismatch():
    """Test boundary: registering a vector with incorrect dimension raises ValueError."""
    engine = DeterministicEmbeddingEngine(dimension=768)
    invalid_dimension_vector = [0.1] * 384

    with pytest.raises(ValueError):
        engine.register_custom_vector("test text", invalid_dimension_vector)


@pytest.mark.tier2
@pytest.mark.feature("F19")
@pytest.mark.asyncio
async def test_f19_boundary_embedding_client_upstream_error():
    """Test boundary: upstream embedding server errors raise EmbeddingException from the client."""
    from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
    from tests.e2e.harness.mock_server import MockServerManager

    mock_mgr = MockServerManager()
    await mock_mgr.start()
    try:
        mock_mgr.embedding.queue_error(502, "Embedding service temporarily unavailable")
        client = HTTPEmbeddingClient(
            base_url=mock_mgr.embedding_url,
            model_id="bge-large",
            dimension=1024,
            resilience_policy=ResiliencePolicy(max_attempts=1),
        )
        with pytest.raises(EmbeddingException) as exc_info:
            await client.embed_texts(["sample text"])
        assert exc_info.value.status_code == 502
        assert exc_info.value.message == "Embedding provider request failed."
        assert "temporarily unavailable" not in exc_info.value.message
    finally:
        await mock_mgr.stop()


@pytest.mark.tier2
@pytest.mark.feature("F19")
@pytest.mark.asyncio
async def test_f19_boundary_large_batch_embedding_partitioning():
    """Test boundary: 100 texts are embedded correctly and partitioned by batch size."""
    from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
    from tests.e2e.harness.mock_server import MockServerManager

    mock_mgr = MockServerManager(embedding_dimension=128)
    await mock_mgr.start()
    try:
        client = HTTPEmbeddingClient(
            base_url=mock_mgr.embedding_url,
            model_id="bge-small",
            dimension=128,
            batch_size=40,
        )
        texts = [f"Text chunk number {i}" for i in range(100)]
        vectors = await client.embed_texts(texts)

        assert len(vectors) == 100
        assert all(len(v) == 128 for v in vectors)
        # 100 texts with batch_size=40 must partition into ceil(100/40) = 3 calls
        assert mock_mgr.embedding.call_count == 3
        total_recorded = sum(
            len(r.inputs) for r in mock_mgr.embedding.recorded_requests
        )
        assert total_recorded == 100
    finally:
        await mock_mgr.stop()


@pytest.mark.tier2
@pytest.mark.feature("F19")
def test_f19_boundary_embedding_exception_formatting():
    """Test boundary: EmbeddingException structure conforms to Gateway error dictionary."""
    exc = EmbeddingException(
        message="Dimension mismatch: expected 768, got 1536",
        details={"expected_dim": 768, "actual_dim": 1536},
    )
    assert exc.status_code == 502
    assert exc.error_type == "embedding_error"
    d = exc.to_dict()
    assert d["code"] == "embedding_provider_error"
    assert d["details"]["expected_dim"] == 768
