from __future__ import annotations

from dataclasses import dataclass

from src.gateway.application.services.authorized_retrieval_service import AuthorizedRetrievalService
from src.gateway.application.services.runtime_settings_service import EffectiveRuntimePolicy
from src.gateway.application.use_cases.context import UseCaseContext, require_knowledge_tools
from src.gateway.config import get_settings
from src.gateway.domain.authorization import narrow_requested_spaces, resolve_request_space_scope
from src.gateway.domain.retrieval import RetrievalResponse
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.database import get_session_factory
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from src.gateway.infrastructure.runtime_settings_provider import load_active_retrieval_settings


@dataclass(frozen=True, slots=True)
class SearchKnowledgeQuery:
    query: str
    space_ids: tuple[str, ...] | None = None
    active_space_id: str | None = None
    semantic_policy: str = "prefer"
    limit: int = 20
    tags: tuple[str, ...] | None = None


def retrieval_payload(result: RetrievalResponse) -> dict:
    return {
        "query": result.query,
        "hits": [
            {
                "rank": hit.rank,
                "rank_score": hit.rank_score,
                "source_type": hit.candidate.source_type,
                "space_id": hit.candidate.space_id,
                "canonical_id": str(hit.candidate.canonical_id),
                "revision_id": str(hit.candidate.revision_id),
                "title": hit.candidate.title,
                "content_excerpt": hit.candidate.content[:600],
                "citation_uri": hit.candidate.citation_uri,
                "language": hit.candidate.language,
                "source_filename": hit.candidate.source_filename,
                "version": hit.candidate.version,
            }
            for hit in result.hits
        ],
        "health": {
            "semantic_status": result.health.semantic_status,
            "degraded_reasons": list(result.health.degraded_reasons),
            "embedding_generation_id": str(result.health.embedding_generation_id)
            if result.health.embedding_generation_id
            else None,
            "embedding_coverage": result.health.embedding_coverage,
        },
        "explanation": {
            "effective_space_ids": list(result.explanation.effective_space_ids),
            "abstained": result.explanation.abstained,
            "active_space_id": result.explanation.active_space_id,
        },
    }


def redact_retrieval_payload(payload: dict) -> dict:
    payload["health"] = {
        "semantic_status": payload["health"]["semantic_status"],
        "degraded_reasons": [],
        "embedding_generation_id": None,
        "embedding_coverage": None,
    }
    payload["explanation"] = {
        "effective_space_ids": [],
        "abstained": payload["explanation"]["abstained"],
        "active_space_id": None,
    }
    return payload


class RetrievalQueries:
    def __init__(
        self,
        *,
        service_factory=None,
        policy_loader=None,
    ) -> None:
        self._service_factory = service_factory or (
            lambda: AuthorizedRetrievalService(
                PostgresRetrievalUnitRepository(get_session_factory()),
                HTTPEmbeddingClient(),
                runtime_settings_provider=load_active_retrieval_settings,
            )
        )
        self._policy_loader = policy_loader

    async def search(self, ctx: UseCaseContext, query: SearchKnowledgeQuery) -> dict:
        policy = ctx.policy or await self._policy(ctx)
        require_knowledge_tools(policy)
        default_ws = get_settings().gateway.default_workspace_id
        request_scope = resolve_request_space_scope(query.active_space_id, default_ws)
        result = await self._service_factory().search(
            ctx.principal,
            query.query,
            requested_space_ids=narrow_requested_spaces(
                request_scope,
                set(query.space_ids) if query.space_ids is not None else None,
            ),
            active_space_id=query.active_space_id,
            semantic_policy=policy.effective_semantic_policy(query.semantic_policy),
            limit=query.limit,
            tags=list(query.tags) if query.tags is not None else None,
        )
        payload = retrieval_payload(result)
        if not policy.retrieval_explanations_enabled:
            return redact_retrieval_payload(payload)
        return payload

    async def _policy(self, ctx: UseCaseContext) -> EffectiveRuntimePolicy:
        if self._policy_loader is not None:
            return await self._policy_loader(ctx.principal)
        from src.gateway.infrastructure.runtime_settings_provider import load_active_runtime_policy

        return await load_active_runtime_policy(ctx.principal)
