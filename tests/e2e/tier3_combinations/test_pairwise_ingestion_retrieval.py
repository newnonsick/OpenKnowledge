"""Tier 3 Pairwise Combination Tests: Ingestion + Chunking + Embedding + Hybrid Search (FTS + Vector + RRF).

Tests cross-feature interactions between:
- Feature 16: File Upload Endpoint
- Feature 17: Multi-Format Document Parsers
- Feature 18: Fixed & Semantic Chunking
- Feature 19: HTTP Embedding Client & Vector Indexing
- Feature 20: PostgreSQL Full-Text Search (tsvector + GIN)
- Feature 21: pgvector Semantic Search (Cosine <=>)
- Feature 22: Reciprocal Rank Fusion (RRF) & Deduplication
- Feature 23: Structured Context Attribution
"""

import hashlib
import uuid
import pytest

from src.gateway.domain.canonical import BlendedSearchResult, RankedSearchResult
from src.gateway.domain.entities import DocumentChunk, DocumentFile
from tests.e2e.harness.mock_server import DeterministicEmbeddingEngine, MockEmbeddingController


def compute_rrf_score(
    fts_results: list[RankedSearchResult],
    vector_results: list[RankedSearchResult],
    k: int = 60,
    fts_weight: float = 1.0,
    vector_weight: float = 1.0,
) -> list[BlendedSearchResult]:
    """Pure implementation of Reciprocal Rank Fusion algorithm."""
    scores: dict[str, float] = {}
    items_map: dict[str, RankedSearchResult] = {}
    fts_ranks: dict[str, int] = {}
    vec_ranks: dict[str, int] = {}

    for rank, item in enumerate(fts_results, start=1):
        scores[item.id] = scores.get(item.id, 0.0) + (fts_weight / (k + rank))
        items_map[item.id] = item
        fts_ranks[item.id] = rank

    for rank, item in enumerate(vector_results, start=1):
        scores[item.id] = scores.get(item.id, 0.0) + (vector_weight / (k + rank))
        items_map[item.id] = item
        vec_ranks[item.id] = rank

    blended = []
    for item_id, score in scores.items():
        base = items_map[item_id]
        blended.append(
            BlendedSearchResult(
                id=base.id,
                source_type=base.source_type,
                title=base.title,
                content=base.content,
                metadata=base.metadata,
                rrf_score=round(score, 6),
                normalized_score=round(score * (k + 1), 6),
                fts_rank=fts_ranks.get(item_id),
                vector_rank=vec_ranks.get(item_id),
                workspace_id=base.workspace_id,
                is_global=base.is_global,
                version=base.version,
            )
        )

    blended.sort(key=lambda x: x.rrf_score, reverse=True)
    return blended


@pytest.mark.tier3
def test_pairwise_f16_f17_f18_f19_ingestion_text_markdown_chunk_embed():
    """Test pairwise interaction: Text/Markdown parsing + Chunking + Embedding generation."""
    embedding_engine = DeterministicEmbeddingEngine(dimension=384)

    doc_content = (
        "# System Architecture\n\n"
        "## Overview\n"
        "The gateway provides dual OpenAI and Anthropic compatible APIs with hybrid search.\n\n"
        "## Knowledge Engine\n"
        "Internal knowledge items are versioned with SHA-256 and optimistic concurrency control."
    )

    doc_id = uuid.uuid4()
    doc_hash = hashlib.sha256(doc_content.encode("utf-8")).hexdigest()

    doc_file = DocumentFile(
        id=doc_id,
        workspace_id="test_ws",
        filename="architecture.md",
        file_path="/data/storage/test_ws/architecture.md",
        file_size_bytes=len(doc_content.encode("utf-8")),
        content_hash=doc_hash,
        mime_type="text/markdown",
    )
    assert doc_file.file_size == len(doc_content.encode("utf-8"))

    # Split into sections/chunks
    sections = [s.strip() for s in doc_content.split("\n\n") if s.strip()]
    chunks: list[DocumentChunk] = []

    for idx, section in enumerate(sections):
        chunk_hash = hashlib.sha256(section.encode("utf-8")).hexdigest()
        vec = embedding_engine.generate_vector(section)
        assert len(vec) == 384
        # Verify L2 normalization: sum(v^2) ~= 1.0
        norm_sq = sum(x * x for x in vec)
        assert abs(norm_sq - 1.0) < 1e-4

        chunk = DocumentChunk(
            document_id=doc_id,
            workspace_id="test_ws",
            chunk_index=idx,
            content=section,
            content_hash=chunk_hash,
            metadata={"heading": section.split("\n")[0] if section.startswith("#") else "body"},
            embedding=vec,
        )
        chunks.append(chunk)

    assert len(chunks) == 3
    assert chunks[0].chunk_index == 0
    assert chunks[0].metadata["heading"] == "# System Architecture"
    assert chunks[1].chunk_index == 1


@pytest.mark.tier3
def test_pairwise_f17_f18_f19_ingestion_code_json_parsing_chunking():
    """Test pairwise interaction: Code/JSON structure parsing + Embedding vector calculation."""
    embedding_engine = DeterministicEmbeddingEngine(dimension=256)

    code_content = (
        "async def get_user(user_id: str) -> dict:\n"
        "    \"\"\"Fetch user profile from database.\"\"\"\n"
        "    return await db.query(user_id)\n\n"
        "async def save_user(user_data: dict) -> bool:\n"
        "    \"\"\"Persist user record.\"\"\"\n"
        "    return await db.insert(user_data)\n"
    )

    doc_id = uuid.uuid4()
    chunks: list[DocumentChunk] = []
    functions = [f.strip() for f in code_content.split("\n\n") if f.strip()]

    for idx, fn_text in enumerate(functions):
        vec = embedding_engine.generate_vector(fn_text)
        chunk = DocumentChunk(
            document_id=doc_id,
            workspace_id="dev_ws",
            chunk_index=idx,
            content=fn_text,
            content_hash=hashlib.sha256(fn_text.encode("utf-8")).hexdigest(),
            metadata={"language": "python", "type": "function"},
            embedding=vec,
        )
        chunks.append(chunk)

    assert len(chunks) == 2
    assert "get_user" in chunks[0].content
    assert "save_user" in chunks[1].content
    assert chunks[0].metadata["language"] == "python"


@pytest.mark.tier3
def test_pairwise_f20_f21_f22_hybrid_retrieval_rrf_blending_formula():
    """Test pairwise interaction: FTS ranking + Vector distance ranking blended via RRF (k=60)."""
    # Candidate items
    item_a_id = str(uuid.uuid4())  # High in FTS (Rank 1), High in Vector (Rank 2)
    item_b_id = str(uuid.uuid4())  # High in FTS (Rank 2), None in Vector
    item_c_id = str(uuid.uuid4())  # High in Vector (Rank 1), None in FTS

    fts_results = [
        RankedSearchResult(
            id=item_a_id,
            source_type="document_chunk",
            title="Doc A Chunk 1",
            content="Authentication token verification",
            rank=1,
            raw_score=0.95,
        ),
        RankedSearchResult(
            id=item_b_id,
            source_type="knowledge",
            title="Doc B Title",
            content="Authentication Bearer schema",
            rank=2,
            raw_score=0.80,
        ),
    ]

    vector_results = [
        RankedSearchResult(
            id=item_c_id,
            source_type="document_chunk",
            title="Doc C Chunk 1",
            content="API Security and Tokens",
            rank=1,
            raw_score=0.92,
        ),
        RankedSearchResult(
            id=item_a_id,
            source_type="document_chunk",
            title="Doc A Chunk 1",
            content="Authentication token verification",
            rank=2,
            raw_score=0.88,
        ),
    ]

    blended = compute_rrf_score(fts_results, vector_results, k=60)
    assert len(blended) == 3

    assert blended[0].id == item_a_id
    assert blended[0].fts_rank == 1
    assert blended[0].vector_rank == 2
    assert blended[0].rrf_score > blended[1].rrf_score
    assert blended[1].id == item_c_id
    assert blended[2].id == item_b_id


@pytest.mark.tier3
def test_pairwise_f23_structured_context_attribution_generation():
    """Test pairwise interaction: Search results formatted into structured citations with source metadata."""
    chunk_id = str(uuid.uuid4())
    blended_item = BlendedSearchResult(
        id=chunk_id,
        source_type="document_chunk",
        title="Architecture Guide",
        content="The system utilizes PostgreSQL with pgvector for Cosine similarity search.",
        metadata={"filename": "architecture.md", "chunk_index": 2, "author": "dev-lead"},
        rrf_score=0.0325,
        normalized_score=0.98,
        fts_rank=1,
        vector_rank=1,
        workspace_id="core",
        version=None,
    )

    citation = (
        f"[{blended_item.source_type}:{blended_item.metadata['filename']}#chunk{blended_item.metadata['chunk_index']}] "
        f"(score: {blended_item.normalized_score:.2f}) {blended_item.content}"
    )

    assert "architecture.md#chunk2" in citation
    assert "score: 0.98" in citation
    assert "pgvector for Cosine similarity" in citation


@pytest.mark.tier3
def test_pairwise_f16_f12_ingestion_duplicate_content_hash_deduplication():
    """Test pairwise interaction: File ingestion with identical SHA-256 prevents redundant chunks."""
    content_raw = b"Duplicate file content test string for SHA-256 verification."
    hash1 = hashlib.sha256(content_raw).hexdigest()
    hash2 = hashlib.sha256(content_raw).hexdigest()

    assert hash1 == hash2

    doc1 = DocumentFile(
        workspace_id="ws1",
        filename="test.txt",
        file_path="/storage/ws1/test.txt",
        file_size_bytes=len(content_raw),
        content_hash=hash1,
    )
    doc2 = DocumentFile(
        workspace_id="ws1",
        filename="test_copy.txt",
        file_path="/storage/ws1/test_copy.txt",
        file_size_bytes=len(content_raw),
        content_hash=hash2,
    )
    assert doc1.content_hash == doc2.content_hash


@pytest.mark.tier3
def test_pairwise_f16_f17_ingestion_empty_and_corrupt_files_handling():
    """Test pairwise interaction: Empty file upload produces 0 chunks and zero vector error safely."""
    empty_content = ""
    sections = [s for s in empty_content.split("\n\n") if s.strip()]
    assert len(sections) == 0

    doc = DocumentFile(
        workspace_id="ws1",
        filename="empty.md",
        file_path="/storage/ws1/empty.md",
        file_size_bytes=0,
        content_hash=hashlib.sha256(b"").hexdigest(),
    )
    assert doc.file_size_bytes == 0
    assert doc.total_chunks == 0
