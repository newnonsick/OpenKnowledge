from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Sequence
from uuid import UUID

from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.application.ports.retrieval import RetrievalUnitRepository
from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.services.runtime_settings_service import RetrievalRuntimeSettings
from src.gateway.domain.exceptions import AuthorizationException, EmbeddingException
from src.gateway.domain.identity import Principal
from src.gateway.domain.retrieval import RetrievalCandidate, RetrievalExplanation, RetrievalHealth, RetrievalHit, RetrievalResponse, SemanticPolicy
from src.gateway.infrastructure.database import get_session_factory, principal_session
from src.gateway.infrastructure.persistence.principal_context import bind_principal, reset_principal
from src.gateway.observability import increment_metric, observe_metric


ScopeResolver = Callable[[Principal, set[str] | None], Awaitable[tuple[str, ...]]]
RuntimeSettingsProvider = Callable[[Principal], Awaitable[RetrievalRuntimeSettings]]


class AuthorizedRetrievalService:
    def __init__(
        self,
        repository: RetrievalUnitRepository,
        embedding_client: IEmbeddingClient | None,
        *,
        scope_resolver: ScopeResolver | None = None,
        runtime_settings_provider: RuntimeSettingsProvider | None = None,
    ) -> None:
        self._repository = repository
        self._embedding_client = embedding_client
        self._scope_resolver = scope_resolver or self._resolve_scope
        self._runtime_settings_provider = runtime_settings_provider

    async def search(
        self,
        principal: Principal,
        query: str,
        *,
        requested_space_ids: set[str] | None = None,
        active_space_id: str | None = None,
        semantic_policy: SemanticPolicy | None = None,
        limit: int | None = None,
        branch_limit: int | None = None,
        lexical_weight: float | None = None,
        vector_weight: float | None = None,
        rrf_k: int | None = None,
        minimum_lexical_score: float | None = None,
        minimum_vector_similarity: float | None = None,
        max_hits_per_source: int | None = None,
        active_space_boost: float | None = None,
        exact_vector: bool = False,
        hnsw_ef_search: int | None = None,
    ) -> RetrievalResponse:
        normalized_query = query.strip()
        defaults = (
            await self._runtime_settings_provider(principal)
            if self._runtime_settings_provider is not None
            else RetrievalRuntimeSettings()
        )
        semantic_policy = semantic_policy or defaults.semantic_policy
        limit = limit if limit is not None else defaults.limit
        if branch_limit is None and self._runtime_settings_provider is not None:
            branch_limit = defaults.branch_limit
        lexical_weight = defaults.lexical_weight if lexical_weight is None else lexical_weight
        vector_weight = defaults.vector_weight if vector_weight is None else vector_weight
        rrf_k = defaults.rrf_k if rrf_k is None else rrf_k
        minimum_lexical_score = defaults.minimum_lexical_score if minimum_lexical_score is None else minimum_lexical_score
        minimum_vector_similarity = defaults.minimum_vector_similarity if minimum_vector_similarity is None else minimum_vector_similarity
        max_hits_per_source = defaults.max_hits_per_source if max_hits_per_source is None else max_hits_per_source
        active_space_boost = defaults.active_space_boost if active_space_boost is None else active_space_boost
        hnsw_ef_search = defaults.hnsw_ef_search if hnsw_ef_search is None else hnsw_ef_search
        self._validate(
            principal,
            normalized_query,
            semantic_policy,
            limit,
            branch_limit,
            lexical_weight,
            vector_weight,
            rrf_k,
            minimum_lexical_score,
            minimum_vector_similarity,
            max_hits_per_source,
            active_space_boost,
            hnsw_ef_search,
        )
        total_started = perf_counter()
        outcome = "error"
        token = bind_principal(principal)
        try:
            effective_spaces = await self._scope_resolver(principal, requested_space_ids)
            if not effective_spaces:
                response = self._empty_response(normalized_query, effective_spaces, semantic_policy)
                self._observe_response(response)
                outcome = "success"
                return response

            generation_id = await self._repository.active_generation(principal)
            if generation_id is None:
                if semantic_policy == "required":
                    raise EmbeddingException("Semantic retrieval is currently unavailable.")
                response = self._empty_response(
                    normalized_query,
                    effective_spaces,
                    semantic_policy,
                    degraded_reason="active_embedding_generation_unavailable",
                )
                self._observe_response(response)
                outcome = "degraded"
                return response

            resolved_branch_limit = branch_limit or max(limit * 4, 40)
            coverage_task = asyncio.create_task(
                self._repository.generation_coverage(
                    principal,
                    effective_spaces,
                    generation_id,
                )
            )
            lexical_task = None
            if lexical_weight > 0.0:
                lexical_task = asyncio.create_task(
                    self._timed_phase(
                        "fts",
                        lambda: self._repository.lexical_search(
                            principal,
                            effective_spaces,
                            normalized_query,
                            generation_id,
                            resolved_branch_limit,
                            minimum_lexical_score,
                        ),
                    )
                )

            vector_candidates: list[RetrievalCandidate] = []
            degraded_reasons: tuple[str, ...] = ()
            vector_enabled = semantic_policy != "disabled" and vector_weight > 0.0
            if vector_enabled:
                if self._embedding_client is None:
                    if semantic_policy == "required":
                        await self._cancel_tasks(coverage_task, lexical_task)
                        raise EmbeddingException("Semantic retrieval is currently unavailable.")
                    degraded_reasons = ("embedding_provider_unavailable",)
                else:
                    try:
                        query_vector = await self._embedding_client.embed_query(normalized_query)
                    except Exception as exc:
                        if semantic_policy == "required":
                            await self._cancel_tasks(coverage_task, lexical_task)
                            raise EmbeddingException("Semantic retrieval is currently unavailable.") from exc
                        degraded_reasons = ("embedding_provider_unavailable",)
                    else:
                        try:
                            vector_candidates = await self._timed_phase(
                                "vector",
                                lambda: self._repository.vector_search(
                                    principal,
                                    effective_spaces,
                                    query_vector,
                                    generation_id,
                                    resolved_branch_limit,
                                    minimum_vector_similarity,
                                    exact_vector,
                                    hnsw_ef_search,
                                ),
                            )
                        except Exception:
                            await self._cancel_tasks(coverage_task, lexical_task)
                            raise

            try:
                lexical_candidates = await lexical_task if lexical_task is not None else []
            except Exception:
                await self._cancel_tasks(coverage_task)
                raise
            coverage = await coverage_task
            observe_metric("gateway_embedding_generation_coverage_ratio", coverage, outcome="success")
            fusion_started = perf_counter()
            hits = self._fuse(
                lexical_candidates,
                vector_candidates,
                lexical_weight=lexical_weight,
                vector_weight=vector_weight if vector_enabled and not degraded_reasons else 0.0,
                rrf_k=rrf_k,
                limit=limit,
                max_hits_per_source=max_hits_per_source,
                active_space_id=active_space_id if active_space_id in effective_spaces else None,
                active_space_boost=active_space_boost,
            )
            observe_metric("gateway_retrieval_duration_seconds", perf_counter() - fusion_started, phase="fusion", outcome="success")
            if semantic_policy == "disabled" or vector_weight == 0.0:
                semantic_status = "disabled"
            elif degraded_reasons:
                semantic_status = "degraded"
            else:
                semantic_status = "active"
            response = RetrievalResponse(
                query=normalized_query,
                hits=hits,
                health=RetrievalHealth(
                    semantic_status=semantic_status,
                    degraded_reasons=degraded_reasons,
                    embedding_generation_id=generation_id,
                    embedding_coverage=coverage,
                ),
                explanation=RetrievalExplanation(
                    effective_space_ids=effective_spaces,
                    lexical_candidates=tuple(lexical_candidates),
                    vector_candidates=tuple(vector_candidates),
                    source_diversity_limit=max_hits_per_source,
                    active_space_id=active_space_id if active_space_id in effective_spaces else None,
                    abstained=not hits,
                ),
            )
            self._observe_response(response)
            outcome = "degraded" if degraded_reasons else "success"
            return response
        finally:
            reset_principal(token)
            observe_metric("gateway_retrieval_duration_seconds", perf_counter() - total_started, phase="total", outcome=outcome)

    @staticmethod
    async def _timed_phase(phase: str, operation):
        started = perf_counter()
        outcome = "success"
        try:
            return await operation()
        except Exception:
            outcome = "error"
            raise
        finally:
            observe_metric("gateway_retrieval_duration_seconds", perf_counter() - started, phase=phase, outcome=outcome)

    @staticmethod
    def _observe_response(response: RetrievalResponse) -> None:
        result_count = len(response.hits)
        outcome = "zero" if result_count == 0 else "results"
        increment_metric("gateway_retrieval_events_total", event="search", outcome=outcome)
        observe_metric("gateway_retrieval_result_count", result_count, outcome=outcome)
        coverage = 0.0 if result_count == 0 else sum(bool(hit.candidate.citation_uri) for hit in response.hits) / result_count
        observe_metric("gateway_retrieval_citation_coverage_ratio", coverage, outcome=outcome)

    async def _resolve_scope(
        self,
        principal: Principal,
        requested: set[str] | None,
    ) -> tuple[str, ...]:
        try:
            member_id = UUID(principal.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
        async with principal_session(get_session_factory(), principal) as session:
            return await AuthorizationService(session).effective_space_ids(
                member_id,
                requested=requested,
            )

    @staticmethod
    async def _cancel_tasks(*tasks: asyncio.Task | None) -> None:
        active = [task for task in tasks if task is not None]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    @staticmethod
    def _fuse(
        lexical_candidates: Sequence[RetrievalCandidate],
        vector_candidates: Sequence[RetrievalCandidate],
        *,
        lexical_weight: float,
        vector_weight: float,
        rrf_k: int,
        limit: int,
        max_hits_per_source: int,
        active_space_id: str | None,
        active_space_boost: float,
    ) -> tuple[RetrievalHit, ...]:
        candidates: dict[UUID, RetrievalCandidate] = {}
        scores: defaultdict[UUID, float] = defaultdict(float)
        lexical_ranks: dict[UUID, int] = {}
        vector_ranks: dict[UUID, int] = {}
        for weight, branch, ranks in (
            (lexical_weight, lexical_candidates, lexical_ranks),
            (vector_weight, vector_candidates, vector_ranks),
        ):
            if weight == 0.0:
                continue
            seen: set[UUID] = set()
            for rank, candidate in enumerate(branch, start=1):
                if candidate.unit_id in seen:
                    continue
                seen.add(candidate.unit_id)
                candidates.setdefault(candidate.unit_id, candidate)
                ranks[candidate.unit_id] = rank
                scores[candidate.unit_id] += weight / (rrf_k + rank)
        theoretical_max = (lexical_weight + vector_weight) / (rrf_k + 1)
        ordered = sorted(
            scores,
            key=lambda unit_id: (
                -scores[unit_id]
                * (1.0 + active_space_boost if candidates[unit_id].space_id == active_space_id else 1.0),
                str(unit_id),
            ),
        )
        source_counts: defaultdict[str, int] = defaultdict(int)
        selected: list[tuple[UUID, float]] = []
        for unit_id in ordered:
            source_key = candidates[unit_id].source_key
            if source_counts[source_key] >= max_hits_per_source:
                continue
            source_counts[source_key] += 1
            boost = active_space_boost if candidates[unit_id].space_id == active_space_id else 0.0
            selected.append((unit_id, boost))
            if len(selected) == limit:
                break
        return tuple(
            RetrievalHit(
                rank=rank,
                candidate=candidates[unit_id],
                rrf_score=scores[unit_id],
                rank_score=min(1.0, scores[unit_id] * (1.0 + boost) / theoretical_max),
                lexical_rank=lexical_ranks.get(unit_id),
                vector_rank=vector_ranks.get(unit_id),
                active_space_boost=boost,
            )
            for rank, (unit_id, boost) in enumerate(selected, start=1)
        )

    @staticmethod
    def _empty_response(
        query: str,
        effective_spaces: tuple[str, ...],
        semantic_policy: SemanticPolicy,
        degraded_reason: str | None = None,
    ) -> RetrievalResponse:
        if degraded_reason is not None:
            semantic_status = "degraded"
            reasons = (degraded_reason,)
        elif semantic_policy == "disabled":
            semantic_status = "disabled"
            reasons = ()
        else:
            semantic_status = "active"
            reasons = ()
        return RetrievalResponse(
            query=query,
            hits=(),
            health=RetrievalHealth(
                semantic_status=semantic_status,
                degraded_reasons=reasons,
                embedding_generation_id=None,
                embedding_coverage=None,
            ),
            explanation=RetrievalExplanation(
                effective_space_ids=effective_spaces,
                lexical_candidates=(),
                vector_candidates=(),
                source_diversity_limit=0,
                active_space_id=None,
                abstained=True,
            ),
        )

    @staticmethod
    def _validate(
        principal: Principal,
        query: str,
        semantic_policy: str,
        limit: int,
        branch_limit: int | None,
        lexical_weight: float,
        vector_weight: float,
        rrf_k: int,
        minimum_lexical_score: float,
        minimum_vector_similarity: float,
        max_hits_per_source: int,
        active_space_boost: float,
        hnsw_ef_search: int,
    ) -> None:
        if (
            not principal.active
            or principal.restricted
            or ("*" not in principal.scopes and "knowledge:read" not in principal.scopes)
        ):
            raise AuthorizationException()
        if not query or len(query) > 4096:
            raise ValueError("Query is required")
        if semantic_policy not in {"prefer", "required", "disabled"}:
            raise ValueError("Invalid semantic policy")
        values = (lexical_weight, vector_weight, minimum_lexical_score, minimum_vector_similarity, active_space_boost)
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise ValueError("Retrieval weights and thresholds must be finite and non-negative")
        if lexical_weight == 0.0 and vector_weight == 0.0:
            raise ValueError("At least one retrieval branch must be enabled")
        if lexical_weight > 100.0 or vector_weight > 100.0 or active_space_boost > 1.0:
            raise ValueError("Retrieval weights or boosts exceed safe limits")
        if semantic_policy == "required" and vector_weight == 0.0:
            raise ValueError("Semantic retrieval requires a positive vector weight")
        if limit <= 0 or rrf_k <= 0 or max_hits_per_source <= 0 or hnsw_ef_search <= 0:
            raise ValueError("Retrieval limits must be positive")
        if limit > 100 or (branch_limit is not None and (branch_limit <= 0 or branch_limit > 1000)):
            raise ValueError("Retrieval limits exceed safe bounds")
        if minimum_vector_similarity > 1.0:
            raise ValueError("Vector similarity threshold cannot exceed one")
