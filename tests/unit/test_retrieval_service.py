"""Unit tests for Hybrid Retrieval Service (RetrievalService and IRetrievalService)."""

from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.application.ports.repositories import (
    IDocumentRepository,
    IKnowledgeRepository,
)
from src.gateway.application.services.retrieval_service import (
    IRetrievalService,
    RetrievalService,
)
from src.gateway.domain.canonical import BlendedSearchResult, RankedSearchResult


def _create_ranked_result(
    item_id: str,
    title: str,
    content: str,
    raw_score: float,
    source_type: str = "knowledge",
    workspace_id: str = "ws_test",
    is_global: bool = False,
    version: Optional[int] = 1,
) -> RankedSearchResult:
    """Helper to create a RankedSearchResult."""
    return RankedSearchResult(
        id=item_id,
        source_type=source_type,  # type: ignore[arg-type]
        title=title,
        content=content,
        rank=1,
        raw_score=raw_score,
        workspace_id=workspace_id,
        is_global=is_global,
        version=version,
    )


# ---------------------------------------------------------------------------
# 1. Initialization and Interface Verification
# ---------------------------------------------------------------------------


def test_retrieval_service_implements_interface():
    """Verify RetrievalService inherits from IRetrievalService abstract interface."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)
    service = RetrievalService(knowledge_repo=knowledge_repo, document_repo=document_repo)

    assert isinstance(service, IRetrievalService)
    assert service.knowledge_repo is knowledge_repo
    assert service.document_repo is document_repo
    assert service.embedding_client is None


# ---------------------------------------------------------------------------
# 2. search_fts Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_fts_empty_query_returns_empty():
    """Verify empty or whitespace search queries return empty result list."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)
    service = RetrievalService(knowledge_repo=knowledge_repo, document_repo=document_repo)

    res_empty = await service.search_fts(query="", workspace_id="ws1")
    res_ws = await service.search_fts(query="   \t\n", workspace_id="ws1")

    assert res_empty == []
    assert res_ws == []
    knowledge_repo.search_fts.assert_not_called()


@pytest.mark.asyncio
async def test_search_fts_combines_knowledge_and_documents_ranked():
    """Verify search_fts executes knowledge and document searches, sorts by score, and re-indexes ranks."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)

    k_item = _create_ranked_result("k1", "Knowledge Note", "Content K", raw_score=3.0, source_type="knowledge")
    d_item = _create_ranked_result("d1", "Document File", "Content D", raw_score=5.0, source_type="document_chunk")

    knowledge_repo.search_fts = AsyncMock(return_value=[k_item])
    document_repo.search_chunks_fts = AsyncMock(return_value=[d_item])

    service = RetrievalService(knowledge_repo=knowledge_repo, document_repo=document_repo)
    results = await service.search_fts(query="architecture", workspace_id="ws_proj", limit=10)

    assert len(results) == 2
    # d1 has higher raw_score (5.0 vs 3.0), so should be rank 1
    assert results[0].id == "d1"
    assert results[0].rank == 1
    assert results[0].raw_score == 5.0

    assert results[1].id == "k1"
    assert results[1].rank == 2
    assert results[1].raw_score == 3.0

    knowledge_repo.search_fts.assert_called_once_with(query="architecture", workspace_id="ws_proj", limit=10)
    document_repo.search_chunks_fts.assert_called_once_with(query="architecture", workspace_id="ws_proj", limit=10)


@pytest.mark.asyncio
async def test_search_fts_deduplicates_overlapping_items():
    """Verify duplicate item IDs across repositories are deduplicated in FTS results."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)

    item1 = _create_ranked_result("shared_id", "Doc A", "High score", raw_score=10.0)
    item2 = _create_ranked_result("shared_id", "Doc A", "Low score", raw_score=2.0)

    knowledge_repo.search_fts = AsyncMock(return_value=[item1])
    document_repo.search_chunks_fts = AsyncMock(return_value=[item2])

    service = RetrievalService(knowledge_repo=knowledge_repo, document_repo=document_repo)
    results = await service.search_fts(query="test", limit=5)

    assert len(results) == 1
    assert results[0].id == "shared_id"
    assert results[0].raw_score == 10.0


@pytest.mark.asyncio
async def test_search_fts_repository_error_resilience():
    """Verify if one repository throws an error, the other branch succeeds without failing."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)

    d_item = _create_ranked_result("d1", "Document File", "Content D", raw_score=2.0, source_type="document_chunk")
    knowledge_repo.search_fts = AsyncMock(side_effect=Exception("Database connection timeout"))
    document_repo.search_chunks_fts = AsyncMock(return_value=[d_item])

    service = RetrievalService(knowledge_repo=knowledge_repo, document_repo=document_repo)
    results = await service.search_fts(query="query", limit=5)

    assert len(results) == 1
    assert results[0].id == "d1"


# ---------------------------------------------------------------------------
# 3. search_vector Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_vector_no_embedding_client():
    """Verify search_vector returns empty list if no embedding client is configured."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)
    service = RetrievalService(knowledge_repo=knowledge_repo, document_repo=document_repo, embedding_client=None)

    results = await service.search_vector(query="vector search", workspace_id="ws1")
    assert results == []


@pytest.mark.asyncio
async def test_search_vector_empty_query():
    """Verify search_vector returns empty list for empty/whitespace query."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)
    embedding_client = MagicMock(spec=IEmbeddingClient)
    service = RetrievalService(
        knowledge_repo=knowledge_repo,
        document_repo=document_repo,
        embedding_client=embedding_client,
    )

    assert await service.search_vector(query="") == []
    embedding_client.embed_query.assert_not_called()


@pytest.mark.asyncio
async def test_search_vector_embedding_failure_handling():
    """Verify search_vector handles embedding client failure gracefully."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)
    embedding_client = MagicMock(spec=IEmbeddingClient)
    embedding_client.embed_query = AsyncMock(side_effect=Exception("Embedding provider offline"))

    service = RetrievalService(
        knowledge_repo=knowledge_repo,
        document_repo=document_repo,
        embedding_client=embedding_client,
    )
    results = await service.search_vector(query="software architecture")
    assert results == []


@pytest.mark.asyncio
async def test_search_vector_success_flow():
    """Verify search_vector generates query embedding and retrieves ranked vector candidates."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)
    embedding_client = MagicMock(spec=IEmbeddingClient)

    query_vec = [0.1, 0.2, 0.3]
    embedding_client.embed_query = AsyncMock(return_value=query_vec)

    k_vec_item = _create_ranked_result("k_vec", "Knowledge Item", "Semantics K", raw_score=0.92)
    d_vec_item = _create_ranked_result("d_vec", "Document Chunk", "Semantics D", raw_score=0.85)

    knowledge_repo.search_vector = AsyncMock(return_value=[k_vec_item])
    document_repo.search_chunks_vector = AsyncMock(return_value=[d_vec_item])

    service = RetrievalService(
        knowledge_repo=knowledge_repo,
        document_repo=document_repo,
        embedding_client=embedding_client,
    )

    results = await service.search_vector(query="clean design", workspace_id="ws_vec", limit=5)

    assert len(results) == 2
    assert results[0].id == "k_vec"
    assert results[0].rank == 1
    assert results[1].id == "d_vec"
    assert results[1].rank == 2

    embedding_client.embed_query.assert_called_once_with("clean design")
    knowledge_repo.search_vector.assert_called_once_with(query_vector=query_vec, workspace_id="ws_vec", limit=10)
    document_repo.search_chunks_vector.assert_called_once_with(query_vector=query_vec, workspace_id="ws_vec", limit=10)


# ---------------------------------------------------------------------------
# 4. hybrid_search and RRF Fusion Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_search_empty_query_and_limit():
    """Verify hybrid_search handles empty query and limit <= 0."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)
    service = RetrievalService(knowledge_repo=knowledge_repo, document_repo=document_repo)

    assert await service.hybrid_search(query="", workspace_id="ws1") == []
    assert await service.hybrid_search(query="test", limit=0) == []
    assert await service.hybrid_search(query="test", limit=-1) == []


@pytest.mark.asyncio
async def test_hybrid_search_end_to_end_fusion():
    """Verify hybrid_search coordinates parallel FTS + Vector searches and blends via RRF."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)
    embedding_client = MagicMock(spec=IEmbeddingClient)

    # Item A: in FTS (Rank 1) and in Vector (Rank 2)
    # Item B: in FTS (Rank 2) only
    # Item C: in Vector (Rank 1) only
    item_a = _create_ranked_result("item_a", "Doc A", "Content A", raw_score=0.95)
    item_b = _create_ranked_result("item_b", "Doc B", "Content B", raw_score=0.80)
    item_c = _create_ranked_result("item_c", "Doc C", "Content C", raw_score=0.98)

    # Setup FTS returns [item_a, item_b]
    knowledge_repo.search_fts = AsyncMock(return_value=[item_a, item_b])
    document_repo.search_chunks_fts = AsyncMock(return_value=[])

    # Setup Vector returns [item_c, item_a]
    embedding_client.embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])
    knowledge_repo.search_vector = AsyncMock(return_value=[item_c, item_a])
    document_repo.search_chunks_vector = AsyncMock(return_value=[])

    service = RetrievalService(
        knowledge_repo=knowledge_repo,
        document_repo=document_repo,
        embedding_client=embedding_client,
    )

    blended = await service.hybrid_search(
        query="architecture",
        workspace_id="ws_hybrid",
        limit=5,
        rrf_k=60,
    )

    assert len(blended) == 3
    # item_a is in both lists -> highest RRF score
    assert blended[0].id == "item_a"
    assert blended[0].fts_rank == 1
    assert blended[0].vector_rank == 2
    assert blended[0].normalized_score == pytest.approx(0.9919)

    # Remaining items
    ids = [b.id for b in blended]
    assert "item_b" in ids
    assert "item_c" in ids


@pytest.mark.asyncio
async def test_hybrid_search_partial_failure_graceful_degradation(caplog):
    """Verify that if vector search fails, hybrid search still succeeds using FTS candidates."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)
    embedding_client = MagicMock(spec=IEmbeddingClient)

    # Embedding client fails
    sentinel = "sentinel-provider-secret-body"
    embedding_client.embed_query = AsyncMock(side_effect=Exception(sentinel))

    # FTS succeeds
    item_fts = _create_ranked_result("fts_only", "FTS Doc", "FTS Content", raw_score=2.5)
    knowledge_repo.search_fts = AsyncMock(return_value=[item_fts])
    document_repo.search_chunks_fts = AsyncMock(return_value=[])

    service = RetrievalService(
        knowledge_repo=knowledge_repo,
        document_repo=document_repo,
        embedding_client=embedding_client,
    )
    service.search_vector = AsyncMock(side_effect=Exception(sentinel))

    blended = await service.hybrid_search(query="resilience test", limit=5)
    assert len(blended) == 1
    assert blended[0].id == "fts_only"
    assert blended[0].fts_rank == 1
    assert blended[0].vector_rank is None
    assert blended[0].normalized_score == 0.5
    assert sentinel not in caplog.text


# ---------------------------------------------------------------------------
# 5. format_context and get_relevant_context Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_format_context_and_get_relevant_context():
    """Verify get_relevant_context performs hybrid search and produces formatted context string."""
    knowledge_repo = MagicMock(spec=IKnowledgeRepository)
    document_repo = MagicMock(spec=IDocumentRepository)

    item = _create_ranked_result(
        "k_context",
        "PEP 8 Style Guide",
        "Write clean, readable code.",
        raw_score=1.0,
        workspace_id="ws_py",
        version=1,
    )
    knowledge_repo.search_fts = AsyncMock(return_value=[item])
    document_repo.search_chunks_fts = AsyncMock(return_value=[])

    service = RetrievalService(knowledge_repo=knowledge_repo, document_repo=document_repo)

    context_str = await service.get_relevant_context(query="pep8 guidelines", workspace_id="ws_py")

    assert "[1] Source: PEP 8 Style Guide" in context_str
    assert "Workspace: ws_py" in context_str
    assert "Write clean, readable code." in context_str
