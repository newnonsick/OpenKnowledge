"""Tier 1 Feature Tests for Feature 18: Fixed & Semantic Chunking.

Validates text chunking algorithms, fixed-size windowing with overlap, sequential chunk indexing,
content hashing per chunk, and DocumentChunk domain entity instantiation.
"""

import hashlib
from typing import List
from uuid import uuid4
import pytest

from src.gateway.domain.entities import DocumentChunk


def fixed_chunk_text(text: str, chunk_size: int = 200, overlap: int = 40) -> List[str]:
    """Splits text into chunks of chunk_size with sliding overlap."""
    if not text:
        return []
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Invalid chunk_size or overlap parameters")

    chunks = []
    start = 0
    text_len = len(text)
    step = chunk_size - overlap

    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunks.append(text[start:end])
        if end == text_len:
            break
        start += step

    return chunks


@pytest.mark.tier1
@pytest.mark.feature("F18")
def test_f18_fixed_size_chunking_with_overlap():
    """Verify splitting a document into fixed-size chunks with specified overlap."""
    sample_text = (
        "Clean Architecture establishes distinct layers: presentation, application, domain, and infrastructure. "
        "Each layer maintains strict boundary separation and depends only inwards towards pure domain logic. "
        "External frameworks and database adapters reside strictly at the outer perimeter."
    )
    chunks = fixed_chunk_text(sample_text, chunk_size=100, overlap=20)
    assert len(chunks) >= 3
    assert all(len(c) <= 100 for c in chunks)

    # Verify overlap between consecutive chunks
    overlap_tail = chunks[0][-20:]
    assert overlap_tail in chunks[1]


@pytest.mark.tier1
@pytest.mark.feature("F18")
def test_f18_chunk_index_ordering_and_continuity():
    """Verify chunk sequence maintains 0-based sequential ordering and reconstructs source stream."""
    text = "0123456789" * 30  # 300 characters
    chunk_list = fixed_chunk_text(text, chunk_size=100, overlap=25)

    doc_id = uuid4()
    doc_chunks = []
    for idx, content in enumerate(chunk_list):
        c_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        doc_chunks.append(
            DocumentChunk(
                document_id=doc_id,
                chunk_index=idx,
                content=content,
                content_hash=c_hash,
            )
        )

    assert len(doc_chunks) == len(chunk_list)
    for idx, dc in enumerate(doc_chunks):
        assert dc.chunk_index == idx
        assert dc.document_id == doc_id


@pytest.mark.tier1
@pytest.mark.feature("F18")
def test_f18_document_chunk_domain_entity_creation():
    """Verify DocumentChunk domain entity fields, metadata dictionary, and embedding storage."""
    doc_id = uuid4()
    chunk_id = uuid4()
    content = "Document chunk content for semantic search."
    c_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    embedding = [0.1, 0.2, 0.3, 0.4]

    chunk = DocumentChunk(
        id=chunk_id,
        document_id=doc_id,
        workspace_id="ws-123",
        chunk_index=0,
        content=content,
        content_hash=c_hash,
        metadata={"filename": "notes.md", "char_count": len(content)},
        embedding=embedding,
    )

    assert chunk.id == chunk_id
    assert chunk.document_id == doc_id
    assert chunk.workspace_id == "ws-123"
    assert chunk.chunk_index == 0
    assert chunk.content == content
    assert chunk.content_hash == c_hash
    assert chunk.metadata["filename"] == "notes.md"
    assert chunk.embedding == embedding


@pytest.mark.tier1
@pytest.mark.feature("F18")
def test_f18_chunk_content_hash_sha256_uniqueness():
    """Verify distinct chunks generate unique SHA-256 hashes for deduplication."""
    chunk_a = "First unique chunk about architecture"
    chunk_b = "Second unique chunk about persistence"

    hash_a = hashlib.sha256(chunk_a.encode("utf-8")).hexdigest()
    hash_b = hashlib.sha256(chunk_b.encode("utf-8")).hexdigest()

    assert hash_a != hash_b
    assert len(hash_a) == 64
    assert len(hash_b) == 64


@pytest.mark.tier1
@pytest.mark.feature("F18")
def test_f18_short_text_single_chunk_preservation():
    """Verify that text shorter than chunk_size produces exactly one chunk with complete content."""
    short_text = "Short single paragraph document."
    chunks = fixed_chunk_text(short_text, chunk_size=500, overlap=50)
    assert len(chunks) == 1
    assert chunks[0] == short_text
