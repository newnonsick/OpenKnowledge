"""Tier 1 Feature Tests for Feature 19: HTTP Embedding Client & Vector Indexing.

Validates the REAL HTTPEmbeddingClient against the in-process mock embedding
upstream: request/response handling, batching, dimension validation, and TEI
endpoint compatibility. The deterministic engine tests cover the mock harness
vector generator used across the suite.
"""

import math
import pytest

from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from tests.e2e.harness.mock_server import DeterministicEmbeddingEngine, MockServerManager


@pytest.mark.tier1
@pytest.mark.feature("F19")
def test_f19_deterministic_embedding_engine_unit_norm():
    """Verify that generated embedding vectors are strictly unit-normalized (L2 norm == 1.0)."""
    engine = DeterministicEmbeddingEngine(dimension=768)
    vector = engine.generate_vector("Pragmatic clean architecture vector test")

    assert len(vector) == 768
    l2_norm = math.sqrt(sum(x * x for x in vector))
    assert abs(l2_norm - 1.0) < 1e-4


@pytest.mark.tier1
@pytest.mark.feature("F19")
@pytest.mark.asyncio
async def test_f19_embedding_client_single_text_request():
    """Verify the embedding client fetches a valid vector for a single text input."""
    mock_mgr = MockServerManager(embedding_dimension=384)
    await mock_mgr.start()
    try:
        client = HTTPEmbeddingClient(
            base_url=mock_mgr.embedding_url,
            model_id="BAAI/bge-large-en-v1.5",
            dimension=384,
        )
        vectors = await client.embed_texts(["Embedding search query"])
        assert len(vectors) == 1
        assert len(vectors[0]) == 384
        assert mock_mgr.embedding.call_count == 1
        assert mock_mgr.embedding.last_request.inputs == ["Embedding search query"]
    finally:
        await mock_mgr.stop()


@pytest.mark.tier1
@pytest.mark.feature("F19")
@pytest.mark.asyncio
async def test_f19_batch_text_embedding_single_request():
    """Verify batch embedding generation sends multiple texts in a single HTTP call."""
    mock_mgr = MockServerManager(embedding_dimension=256)
    await mock_mgr.start()
    try:
        client = HTTPEmbeddingClient(
            base_url=mock_mgr.embedding_url,
            model_id="test-embed-model",
            dimension=256,
            batch_size=32,
        )
        inputs = [
            "First document chunk",
            "Second document chunk",
            "Third document chunk",
        ]
        vectors = await client.embed_texts(inputs)
        assert len(vectors) == 3
        for vec in vectors:
            assert len(vec) == 256
        # A single batched HTTP call carries all three inputs
        assert mock_mgr.embedding.call_count == 1
        assert mock_mgr.embedding.last_request.inputs == inputs
    finally:
        await mock_mgr.stop()


@pytest.mark.tier1
@pytest.mark.feature("F19")
@pytest.mark.asyncio
async def test_f19_embedding_client_partitioning_over_batch_size():
    """Verify the client partitions inputs larger than batch_size into several calls."""
    mock_mgr = MockServerManager(embedding_dimension=128)
    await mock_mgr.start()
    try:
        client = HTTPEmbeddingClient(
            base_url=mock_mgr.embedding_url,
            model_id="test-embed-model",
            dimension=128,
            batch_size=2,
        )
        vectors = await client.embed_texts(["a", "b", "c", "d", "e"])
        assert len(vectors) == 5
        # 5 inputs with batch_size=2 must be split into 3 HTTP requests
        assert mock_mgr.embedding.call_count == 3
        assert mock_mgr.embedding.recorded_requests[0].inputs == ["a", "b"]
        assert mock_mgr.embedding.recorded_requests[-1].inputs == ["e"]
    finally:
        await mock_mgr.stop()


@pytest.mark.tier1
@pytest.mark.feature("F19")
def test_f19_embedding_dimension_configuration():
    """Verify embedding engine respects configured dimension sizes (e.g. 128, 384, 1024)."""
    for dim in [128, 384, 1024]:
        engine = DeterministicEmbeddingEngine(dimension=dim)
        vec = engine.generate_vector("Dimension test string")
        assert len(vec) == dim
        assert engine.dimension == dim


@pytest.mark.tier1
@pytest.mark.feature("F19")
@pytest.mark.asyncio
async def test_f19_tei_compatible_embed_endpoint():
    """Verify the client targets TEI's /embed route when the base URL points at it."""
    mock_mgr = MockServerManager(embedding_dimension=512)
    await mock_mgr.start()
    try:
        client = HTTPEmbeddingClient(
            base_url=f"{mock_mgr.base_url}/embed",
            model_id="bge-small",
            dimension=512,
        )
        vectors = await client.embed_texts(["TEI compatibility test"])
        assert len(vectors) == 1
        assert len(vectors[0]) == 512
        # The mock records the actual endpoint path hit
        assert mock_mgr.embedding.last_request.endpoint == "/embed"
    finally:
        await mock_mgr.stop()


@pytest.mark.tier1
@pytest.mark.feature("F19")
@pytest.mark.asyncio
async def test_f19_embedding_client_dimension_mismatch_detection():
    """Verify the client rejects vectors whose dimension contradicts the configuration."""
    from src.gateway.domain.exceptions import EmbeddingException

    mock_mgr = MockServerManager(embedding_dimension=256)
    await mock_mgr.start()
    try:
        client = HTTPEmbeddingClient(
            base_url=mock_mgr.embedding_url,
            model_id="mismatch-model",
            dimension=1024,  # configured 1024 but the backend returns 256
        )
        with pytest.raises(EmbeddingException) as exc_info:
            await client.embed_texts(["dimension mismatch probe"])
        assert "Dimension mismatch" in exc_info.value.message
    finally:
        await mock_mgr.stop()
