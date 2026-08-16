

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Set

from src.gateway.domain.canonical import BlendedSearchResult, RankedSearchResult

logger = logging.getLogger(__name__)

DEFAULT_RRF_K: int = 60

def sanitize_tsquery(raw_query: str) -> str:

    if not raw_query or not raw_query.strip():
        return ""
    cleaned = re.sub(r"[!&|():*'\";\-]+", " ", raw_query)
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return ""
    return " & ".join(tokens)

def compute_rrf(
    ranked_lists: Sequence[Sequence[RankedSearchResult]],
    weights: Optional[Sequence[float]] = None,
    rrf_k: int = DEFAULT_RRF_K,
    limit: int = 10,
) -> List[BlendedSearchResult]:

    if limit <= 0:
        return []

    if rrf_k <= 0:
        rrf_k = DEFAULT_RRF_K

    if not ranked_lists:
        return []

    resolved_weights: List[float] = []
    for i in range(len(ranked_lists)):
        if weights is not None and i < len(weights) and weights[i] > 0.0:
            resolved_weights.append(float(weights[i]))
        else:
            resolved_weights.append(1.0)

    scores: Dict[str, float] = {}
    items_map: Dict[str, RankedSearchResult] = {}
    fts_ranks: Dict[str, int] = {}
    vector_ranks: Dict[str, int] = {}
    seen_in_list: Set[str] = set()

    for list_idx, rank_list in enumerate(ranked_lists):
        if not rank_list:
            continue
        weight = resolved_weights[list_idx]
        seen_in_list.clear()

        for rank_idx, item in enumerate(rank_list, start=1):
            doc_id = str(item.id)
            if doc_id in seen_in_list:

                continue
            seen_in_list.add(doc_id)

            if doc_id not in items_map:
                items_map[doc_id] = item
                scores[doc_id] = 0.0

            rrf_score = weight / (rrf_k + rank_idx)
            scores[doc_id] += rrf_score

            if list_idx == 0:
                fts_ranks[doc_id] = rank_idx
            elif list_idx == 1:
                vector_ranks[doc_id] = rank_idx

    if not scores:
        return []

    sorted_docs = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    max_score = sorted_docs[0][1] if sorted_docs else 1.0

    blended: List[BlendedSearchResult] = []
    for doc_id, raw_rrf in sorted_docs:
        orig = items_map[doc_id]
        normalized = (raw_rrf / max_score) if max_score > 0 else 0.0
        blended.append(
            BlendedSearchResult(
                id=orig.id,
                source_type=orig.source_type,
                title=orig.title,
                content=orig.content,
                metadata=dict(orig.metadata) if orig.metadata else {},
                rrf_score=round(raw_rrf, 6),
                normalized_score=round(normalized, 4),
                fts_rank=fts_ranks.get(doc_id),
                vector_rank=vector_ranks.get(doc_id),
                workspace_id=orig.workspace_id,
                is_global=orig.is_global,
                version=orig.version,
            )
        )

    return blended

def compute_rrf_fusion(
    ranked_lists: Sequence[Sequence[RankedSearchResult]],
    rrf_k: int = DEFAULT_RRF_K,
    limit: int = 10,
) -> List[BlendedSearchResult]:

    return compute_rrf(ranked_lists=ranked_lists, weights=None, rrf_k=rrf_k, limit=limit)

def compute_rrf_score(
    fts_results: Sequence[RankedSearchResult],
    vector_results: Sequence[RankedSearchResult],
    k: int = DEFAULT_RRF_K,
    fts_weight: float = 1.0,
    vector_weight: float = 1.0,
    limit: int = 10,
) -> List[BlendedSearchResult]:

    return compute_rrf(
        ranked_lists=[fts_results, vector_results],
        weights=[fts_weight, vector_weight],
        rrf_k=k,
        limit=limit,
    )

def format_context_attribution(
    results: Sequence[BlendedSearchResult],
    max_snippet_len: int = 500,
) -> str:

    if not results:
        return "No relevant internal knowledge found."

    blocks: List[str] = []
    for idx, item in enumerate(results, start=1):
        content = item.content or ""
        if len(content) > max_snippet_len:
            content = content[:max_snippet_len].rstrip() + "... [truncated]"

        scope = "Global" if item.is_global else f"Workspace: {item.workspace_id}"
        version_str = f"v{item.version}" if item.version is not None else "latest"
        header = f"[{idx}] Source: {item.title} ({item.source_type}, {version_str}, {scope}, Relevancy: {item.normalized_score:.2f})"
        blocks.append(f"{header}\n{content}")

    return "\n\n---\n\n".join(blocks)

def format_citation(item: BlendedSearchResult) -> str:

    version_str = f"v{item.version}" if item.version is not None else "latest"
    return f"[Source: {item.title} | ID: {item.id} | Revision: {version_str} | Score: {item.normalized_score:.2f}]"
