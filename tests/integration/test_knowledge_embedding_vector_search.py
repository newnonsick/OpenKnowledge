"""Integration tests for Knowledge embedding persistence, vector similarity search, and hybrid retrieval."""

from __future__ import annotations

import json
from uuid import UUID, uuid4
import pytest

from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.application.services.ingestion_service import IngestionService
from src.gateway.application.services.knowledge_service import KnowledgeService
from src.gateway.application.services.retrieval_service import RetrievalService
from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.persistence.document_repository import DocumentRepository
from src.gateway.infrastructure.persistence.knowledge_repository import KnowledgeRepository
from src.gateway.infrastructure.persistence.models import EMBED_DIM
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter
from tests.e2e.harness.test_env import TestEnvironment


class FakeEmbeddingClient(IEmbeddingClient):
    """Deterministic embedding client for integration tests."""

    def __init__(self, dimension: int = EMBED_DIM) -> None:
        self._dimension = dimension
        self.call_history: list[list[str]] = []

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.call_history.append(list(texts))
        results: list[list[float]] = []
        for text in texts:
            t_lower = text.lower()
            if "database" in t_lower or "sql" in t_lower:
                vec = [0.9, 0.1, 0.0] + [0.0] * (self._dimension - 3)
            elif "frontend" in t_lower or "react" in t_lower or "css" in t_lower:
                vec = [0.0, 0.9, 0.1] + [0.0] * (self._dimension - 3)
            else:
                vec = [0.1, 0.1, 0.8] + [0.0] * (self._dimension - 3)
            results.append(vec)
        return results

    async def embed_query(self, query: str) -> list[float]:
        vectors = await self.embed_texts([query])
        return vectors[0]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_knowledge_service_embedding_lifecycle_persistence():
    """Verify that KnowledgeService saves, updates, and retrieves embeddings in DB."""
    async with TestEnvironment() as env:
        emb_client = FakeEmbeddingClient(dimension=EMBED_DIM)
        k_repo = KnowledgeRepository(session_factory=env.session_factory)
        service = KnowledgeService(repository=k_repo, embedding_client=emb_client)

        # 1. Save item -> v1 revision with embedding persisted
        item = await service.save_item(
            title="Database Best Practices",
            content="Use PostgreSQL with pgvector for efficient similarity search.",
            workspace_id="test_ws",
            author="engineer",
        )
        assert item.version == 1
        assert item.current_revision is not None
        assert item.current_revision.embedding is not None
        assert len(item.current_revision.embedding) == EMBED_DIM
        # First element is 0.9 because content contains 'database'/'sql'
        assert item.current_revision.embedding[0] == 0.9

        # 2. Verify reading from repository directly hydrates embedding
        fetched = await service.get_item(item.id, workspace_id="test_ws")
        assert fetched is not None
        assert fetched.current_revision is not None
        assert fetched.current_revision.embedding is not None
        assert fetched.current_revision.embedding[0] == 0.9

        # 3. Update item OCC to v2 with frontend content and title
        updated = await service.update_item(
            item_id=item.id,
            title="Frontend Components",
            content="Frontend UI components using React and CSS.",
            expected_version=1,
            workspace_id="test_ws",
            author="engineer",
        )
        assert updated.version == 2
        assert updated.title == "Frontend Components"
        assert updated.current_revision is not None
        assert updated.current_revision.embedding is not None
        # v2 embedding should reflect frontend content (second dimension 0.9)
        assert updated.current_revision.embedding[1] == 0.9

        # 4. Verify historical revisions preserve respective embeddings
        revisions = await service.list_revisions(item.id)
        assert len(revisions) == 2
        assert revisions[0].version == 1
        assert revisions[0].embedding is not None
        assert revisions[0].embedding[0] == 0.9  # v1 database embedding
        assert revisions[1].version == 2
        assert revisions[1].embedding is not None
        assert revisions[1].embedding[1] == 0.9  # v2 frontend embedding


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_search_blends_knowledge_and_documents():
    """Verify Hybrid Retrieval fuses vector and FTS results from both knowledge items and document chunks."""
    async with TestEnvironment() as env:
        emb_client = FakeEmbeddingClient(dimension=EMBED_DIM)
        k_repo = KnowledgeRepository(session_factory=env.session_factory)
        d_repo = DocumentRepository(session_factory=env.session_factory)
        storage = LocalStorageAdapter(base_dir=env.storage_path)

        k_service = KnowledgeService(repository=k_repo, embedding_client=emb_client)
        ingestion = IngestionService(
            storage=storage,
            document_repository=d_repo,
            embedding_client=emb_client,
            chunk_size=200,
        )
        retrieval = RetrievalService(
            knowledge_repo=k_repo,
            document_repo=d_repo,
            embedding_client=emb_client,
        )

        # 1. Save a knowledge item about databases
        k_item = await k_service.save_item(
            title="SQL Tuning Guide",
            content="Database indexes and SQL query optimization strategies.",
            workspace_id="ws_hybrid",
        )

        # 2. Ingest a document file about databases
        doc_content = b"PostgreSQL database query planner and vacuum maintenance."
        doc_file = await ingestion.ingest_file(
            workspace_id="ws_hybrid",
            filename="postgres_guide.txt",
            content=doc_content,
        )

        # 3. Perform hybrid search for 'database SQL query'
        results = await retrieval.hybrid_search(
            query="database SQL query",
            workspace_id="ws_hybrid",
            limit=5,
        )

        assert len(results) >= 2
        source_types = {r.source_type for r in results}
        assert "knowledge" in source_types
        assert "document_chunk" in source_types

        # Verify context formatting
        context_str = retrieval.format_context(results)
        assert "SQL Tuning Guide" in context_str
        assert "postgres_guide.txt" in context_str
