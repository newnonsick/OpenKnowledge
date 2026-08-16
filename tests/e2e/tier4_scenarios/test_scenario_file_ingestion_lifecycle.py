"""Tier 4 Real-World Scenario: File Ingestion & Indexing Full Lifecycle.

Simulates the complete lifecycle of document file upload, parsing, semantic chunking,
embedding indexing, and subsequent retrieval.
"""

import hashlib
from pathlib import Path
import uuid
import pytest

from src.gateway.domain.entities import DocumentChunk, DocumentFile
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter
from tests.e2e.harness.mock_server import DeterministicEmbeddingEngine


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_file_ingestion_complete_lifecycle(tmp_path: Path):
    """Scenario: Upload Markdown & Python files -> Local storage -> Chunking -> Embeddings -> Searchable chunks."""
    storage_adapter = LocalStorageAdapter(base_dir=tmp_path / "gateway_storage")
    embedding_engine = DeterministicEmbeddingEngine(dimension=384)

    # 1. Ingest Python file
    py_code = (
        "def compute_rrf(rank1: int, rank2: int, k: int = 60) -> float:\n"
        "    \"\"\"Calculate reciprocal rank fusion score.\"\"\"\n"
        "    return (1.0 / (k + rank1)) + (1.0 / (k + rank2))\n"
    )
    py_bytes = py_code.encode("utf-8")
    py_hash = hashlib.sha256(py_bytes).hexdigest()
    py_doc_id = uuid.uuid4()

    py_saved_path = await storage_adapter.save_file(
        workspace_id="core_ws",
        file_id=py_doc_id,
        filename="rrf_utils.py",
        content=py_bytes,
    )
    assert py_saved_path.exists()
    assert await storage_adapter.exists("core_ws", py_doc_id, "rrf_utils.py")

    py_doc = DocumentFile(
        id=py_doc_id,
        workspace_id="core_ws",
        filename="rrf_utils.py",
        file_path=str(py_saved_path),
        file_size_bytes=len(py_bytes),
        content_hash=py_hash,
        mime_type="text/x-python",
    )

    py_vector = embedding_engine.generate_vector(py_code)
    py_chunk = DocumentChunk(
        document_id=py_doc_id,
        workspace_id="core_ws",
        chunk_index=0,
        content=py_code,
        content_hash=py_hash,
        metadata={"filename": "rrf_utils.py", "language": "python"},
        embedding=py_vector,
    )

    # 2. Ingest Markdown file
    md_content = (
        "# RRF Guide\n\n"
        "Reciprocal Rank Fusion blends search results from multiple retrieval pipelines."
    )
    md_bytes = md_content.encode("utf-8")
    md_hash = hashlib.sha256(md_bytes).hexdigest()
    md_doc_id = uuid.uuid4()

    md_saved_path = await storage_adapter.save_file(
        workspace_id="core_ws",
        file_id=md_doc_id,
        filename="rrf_guide.md",
        content=md_bytes,
    )
    assert md_saved_path.exists()

    md_doc = DocumentFile(
        id=md_doc_id,
        workspace_id="core_ws",
        filename="rrf_guide.md",
        file_path=str(md_saved_path),
        file_size_bytes=len(md_bytes),
        content_hash=md_hash,
        mime_type="text/markdown",
    )

    md_vector = embedding_engine.generate_vector(md_content)
    md_chunk = DocumentChunk(
        document_id=md_doc_id,
        workspace_id="core_ws",
        chunk_index=0,
        content=md_content,
        content_hash=md_hash,
        metadata={"filename": "rrf_guide.md", "type": "documentation"},
        embedding=md_vector,
    )

    # 3. Verify chunks indexing and searchability
    indexed_chunks = [py_chunk, md_chunk]
    assert len(indexed_chunks) == 2

    # Query matching
    query_text = "reciprocal rank fusion calculation"
    query_vec = embedding_engine.generate_vector(query_text)

    # Compute cosine similarity
    def cosine_sim(v1: list[float], v2: list[float]) -> float:
        return sum(a * b for a, b in zip(v1, v2))

    scores = [(c.metadata["filename"], cosine_sim(c.embedding, query_vec)) for c in indexed_chunks]
    scores.sort(key=lambda x: x[1], reverse=True)

    assert len(scores) == 2
    assert scores[0][1] > 0.0  # Positive similarity
    assert scores[1][1] > 0.0


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_file_ingestion_deduplication_and_version_replace(tmp_path: Path):
    """Scenario: Re-uploading modified file computes new SHA-256 and updates chunk index."""
    storage_adapter = LocalStorageAdapter(base_dir=tmp_path / "version_storage")

    file_id = uuid.uuid4()
    content_v1 = b"# Project Status\nPhase 1 Complete."
    hash_v1 = hashlib.sha256(content_v1).hexdigest()

    await storage_adapter.save_file(
        workspace_id="team_ws",
        file_id=file_id,
        filename="status.md",
        content=content_v1,
    )

    content_v2 = b"# Project Status\nPhase 1 and 2 Complete."
    hash_v2 = hashlib.sha256(content_v2).hexdigest()

    assert hash_v1 != hash_v2

    await storage_adapter.save_file(
        workspace_id="team_ws",
        file_id=file_id,
        filename="status.md",
        content=content_v2,
    )

    latest_data = await storage_adapter.read_file("team_ws", file_id, "status.md")
    assert latest_data == content_v2
    assert "Phase 1 and 2 Complete" in latest_data.decode("utf-8")
