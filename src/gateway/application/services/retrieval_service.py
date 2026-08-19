

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Any, Dict, List, Optional

from src.gateway.config import get_settings, settings
from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.application.ports.repositories import (
    IDocumentRepository,
    IKnowledgeRepository,
)
from src.gateway.application.services.rrf import (
    DEFAULT_RRF_K,
    compute_rrf,
    format_citation,
    format_context_attribution,
    sanitize_tsquery,
)
from src.gateway.domain.canonical import BlendedSearchResult, RankedSearchResult

logger = logging.getLogger(__name__)

class IRetrievalService(ABC):

    @abstractmethod
    async def hybrid_search(
        self,
        query: str,
        workspace_id: Optional[str] = None,
        limit: int = 10,
        fts_weight: float = 1.0,
        vector_weight: float = 1.0,
        rrf_k: int = 60,
    ) -> List[BlendedSearchResult]:

        ...

class RetrievalService(IRetrievalService):

    def __init__(
        self,
        knowledge_repo: IKnowledgeRepository,
        document_repo: IDocumentRepository,
        embedding_client: Optional[IEmbeddingClient] = None,
    ) -> None:
        self.knowledge_repo = knowledge_repo
        self.document_repo = document_repo
        self.embedding_client = embedding_client

    async def search_fts(
        self,
        query: str,
        workspace_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

        if not query or not query.strip():
            return []

        ws_id = workspace_id or get_settings().gateway.default_workspace_id
        search_limit = max(limit, 10)

        async def _search_knowledge() -> List[RankedSearchResult]:
            try:
                return await self.knowledge_repo.search_fts(
                    query=query,
                    workspace_id=ws_id,
                    limit=search_limit,
                )
            except Exception as exc:
                logger.warning("Knowledge FTS failed", extra={"exception_class": type(exc).__name__})
                return []

        async def _search_documents() -> List[RankedSearchResult]:
            try:
                if hasattr(self.document_repo, "search_chunks_fts"):
                    return await self.document_repo.search_chunks_fts(
                        query=query,
                        workspace_id=ws_id,
                        limit=search_limit,
                    )
                elif hasattr(self.document_repo, "search_fts"):
                    return await self.document_repo.search_fts(
                        query=query,
                        workspace_id=ws_id,
                        limit=search_limit,
                    )
                return []
            except Exception as exc:
                logger.warning("Document FTS failed", extra={"exception_class": type(exc).__name__})
                return []

        k_results, d_results = await asyncio.gather(_search_knowledge(), _search_documents())
        combined = k_results + d_results

        seen_ids = set()
        deduped: List[RankedSearchResult] = []

        combined.sort(key=lambda x: x.raw_score, reverse=True)

        for item in combined:
            item_id = str(item.id)
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            deduped.append(item)

        results: List[RankedSearchResult] = []
        for rank_idx, item in enumerate(deduped[:limit], start=1):
            results.append(
                RankedSearchResult(
                    id=item.id,
                    source_type=item.source_type,
                    title=item.title,
                    content=item.content,
                    metadata=dict(item.metadata) if item.metadata else {},
                    rank=rank_idx,
                    raw_score=item.raw_score,
                    workspace_id=item.workspace_id,
                    is_global=item.is_global,
                    version=item.version,
                )
            )

        return results

    async def search_vector(
        self,
        query: str,
        workspace_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

        if not query or not query.strip():
            return []

        if self.embedding_client is None:
            logger.debug("No embedding client configured; skipping vector search branch.")
            return []

        try:
            query_vector = await self.embedding_client.embed_query(query)
        except Exception as exc:
            logger.warning("Query embedding failed", extra={"exception_class": type(exc).__name__})
            return []

        if not query_vector:
            return []

        ws_id = workspace_id or get_settings().gateway.default_workspace_id
        search_limit = max(limit, 10)

        async def _search_knowledge() -> List[RankedSearchResult]:
            try:
                return await self.knowledge_repo.search_vector(
                    query_vector=query_vector,
                    workspace_id=ws_id,
                    limit=search_limit,
                )
            except Exception as exc:
                logger.warning("Knowledge vector search failed", extra={"exception_class": type(exc).__name__})
                return []

        async def _search_documents() -> List[RankedSearchResult]:
            try:
                if hasattr(self.document_repo, "search_chunks_vector"):
                    return await self.document_repo.search_chunks_vector(
                        query_vector=query_vector,
                        workspace_id=ws_id,
                        limit=search_limit,
                    )
                elif hasattr(self.document_repo, "search_vector"):
                    return await self.document_repo.search_vector(
                        query_vector=query_vector,
                        workspace_id=ws_id,
                        limit=search_limit,
                    )
                return []
            except Exception as exc:
                logger.warning("Document vector search failed", extra={"exception_class": type(exc).__name__})
                return []

        k_results, d_results = await asyncio.gather(_search_knowledge(), _search_documents())
        combined = k_results + d_results

        seen_ids = set()
        deduped: List[RankedSearchResult] = []
        combined.sort(key=lambda x: x.raw_score, reverse=True)

        for item in combined:
            item_id = str(item.id)
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            deduped.append(item)

        results: List[RankedSearchResult] = []
        for rank_idx, item in enumerate(deduped[:limit], start=1):
            results.append(
                RankedSearchResult(
                    id=item.id,
                    source_type=item.source_type,
                    title=item.title,
                    content=item.content,
                    metadata=dict(item.metadata) if item.metadata else {},
                    rank=rank_idx,
                    raw_score=item.raw_score,
                    workspace_id=item.workspace_id,
                    is_global=item.is_global,
                    version=item.version,
                )
            )

        return results

    async def hybrid_search(
        self,
        query: str,
        workspace_id: Optional[str] = None,
        limit: int = 10,
        fts_weight: float = 1.0,
        vector_weight: float = 1.0,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> List[BlendedSearchResult]:

        if not query or not query.strip():
            return []

        if limit <= 0:
            return []

        ws_id = workspace_id or get_settings().gateway.default_workspace_id

        branch_limit = max(limit * 2, 20)

        fts_future = self.search_fts(query=query, workspace_id=ws_id, limit=branch_limit)
        vec_future = self.search_vector(query=query, workspace_id=ws_id, limit=branch_limit)

        fts_res, vec_res = await asyncio.gather(fts_future, vec_future, return_exceptions=True)

        fts_list: List[RankedSearchResult] = fts_res if isinstance(fts_res, list) else []
        vec_list: List[RankedSearchResult] = vec_res if isinstance(vec_res, list) else []

        if isinstance(fts_res, Exception):
            logger.error(
                "FTS search failed in hybrid pipeline",
                extra={"exception_class": type(fts_res).__name__},
            )
        if isinstance(vec_res, Exception):
            logger.error(
                "Vector search failed in hybrid pipeline",
                extra={"exception_class": type(vec_res).__name__},
            )

        return compute_rrf(
            ranked_lists=[fts_list, vec_list],
            weights=[fts_weight, vector_weight],
            rrf_k=rrf_k,
            limit=limit,
        )

    def format_context(
        self,
        results: List[BlendedSearchResult],
        max_snippet_len: int = 500,
    ) -> str:

        return format_context_attribution(results, max_snippet_len=max_snippet_len)

    async def get_relevant_context(
        self,
        query: str,
        workspace_id: Optional[str] = None,
        limit: int = 5,
        max_snippet_len: int = 500,
        fts_weight: float = 1.0,
        vector_weight: float = 1.0,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> str:

        ws_id = workspace_id or get_settings().gateway.default_workspace_id
        results = await self.hybrid_search(
            query=query,
            workspace_id=ws_id,
            limit=limit,
            fts_weight=fts_weight,
            vector_weight=vector_weight,
            rrf_k=rrf_k,
        )
        return self.format_context(results, max_snippet_len=max_snippet_len)
