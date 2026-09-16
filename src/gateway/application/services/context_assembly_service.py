from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.gateway.application.services.authorized_retrieval_service import AuthorizedRetrievalService
from src.gateway.application.services.runtime_settings_service import EffectiveRuntimePolicy
from src.gateway.application.use_cases.context import UseCaseContext, require_knowledge_tools
from src.gateway.config import get_settings
from src.gateway.domain.authorization import narrow_requested_spaces, resolve_request_space_scope
from src.gateway.domain.retrieval import RetrievalCandidate


TOKEN_ESTIMATION_METHOD = "char-based estimate max(1, len(text)//4); per-model tokenizers differ"

STATUS_OK = "ok"
STATUS_OVERSIZED = "oversized"
STATUS_NO_ANSWER = "no_answer"

_WHITESPACE_PATTERN = re.compile(r"\s+")


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def normalize_text(value: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", value.strip().lower())


def candidate_is_superseded(candidate: RetrievalCandidate) -> bool:
    metadata = candidate.source_metadata or {}
    return metadata.get("lifecycle_status") == "superseded" or metadata.get("superseded") is True


def candidate_version(candidate: RetrievalCandidate) -> int:
    version = candidate.version
    if isinstance(version, int):
        return version
    metadata = candidate.source_metadata or {}
    raw_version = metadata.get("version")
    if isinstance(raw_version, int):
        return raw_version
    return 0


@dataclass(frozen=True, slots=True)
class AssembleContextQuery:
    query: str
    space_ids: tuple[str, ...] | None = None
    active_space_id: str | None = None
    semantic_policy: str = "prefer"
    max_sources: int = 8
    max_snippet_chars: int = 600
    max_total_chars: int = 8000
    max_total_tokens: int | None = None
    tags: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class ContextSnippet:
    rank: int
    title: str
    snippet: str
    truncated: bool
    space_id: str
    canonical_id: str
    revision_id: str
    citation_uri: str
    version: int | None
    source_type: str = "knowledge_revision"
    superseded: bool = False


@dataclass(frozen=True, slots=True)
class ContextPackage:
    query: str
    snippets: tuple[ContextSnippet, ...] = ()
    total_chars: int = 0
    budget_chars: int = 0
    total_tokens: int = 0
    budget_tokens: int | None = None
    status: str = STATUS_OK
    omitted_count: int = 0
    omitted_reason: str | None = None
    abstained: bool = False
    degraded: bool = False
    estimation_method: str = TOKEN_ESTIMATION_METHOD
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def query_snippet(content: str, query: str, *, max_chars: int) -> tuple[str, bool]:
    text = " ".join(content.split())
    if len(text) <= max_chars:
        return text, False
    terms = [term.lower() for term in query.split() if term]
    lowered = text.lower()
    best = 0
    for term in terms:
        position = lowered.find(term)
        if position >= 0 and (not best or position < best):
            best = position
    start = max(0, best - max_chars // 3)
    end = min(len(text), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "… " + snippet
    if end < len(text):
        snippet = snippet + " …"
    return snippet, True


class ContextAssembler:
    def __init__(
        self,
        *,
        service_factory=None,
        policy_loader=None,
    ) -> None:
        from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
        from src.gateway.infrastructure.database import get_session_factory
        from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
        from src.gateway.infrastructure.runtime_settings_provider import load_active_retrieval_settings

        self._service_factory = service_factory or (
            lambda: AuthorizedRetrievalService(
                PostgresRetrievalUnitRepository(get_session_factory()),
                HTTPEmbeddingClient(),
                runtime_settings_provider=load_active_retrieval_settings,
            )
        )
        self._policy_loader = policy_loader

    async def assemble(self, ctx: UseCaseContext, query: AssembleContextQuery) -> ContextPackage:
        policy = ctx.policy or await self._policy(ctx)
        require_knowledge_tools(policy)
        if query.max_sources <= 0 or query.max_snippet_chars <= 0 or query.max_total_chars <= 0:
            raise ValueError("Context budgets must be positive")
        if query.max_total_tokens is not None and query.max_total_tokens <= 0:
            raise ValueError("Context budgets must be positive")
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
            limit=max(query.max_sources * 3, query.max_sources),
            tags=list(query.tags) if query.tags is not None else None,
        )
        token_budget = query.max_total_tokens
        seen_sources: set[str] = set()
        seen_texts: set[str] = set()
        freshest: dict[str, RetrievalCandidate] = {}
        for hit in result.hits:
            candidate: RetrievalCandidate = hit.candidate
            key = f"{candidate.source_type}:{candidate.canonical_id}"
            current = freshest.get(key)
            if current is None or candidate_version(candidate) > candidate_version(current):
                freshest[key] = candidate
        ordered_hits = sorted(
            result.hits,
            key=lambda hit: (
                candidate_is_superseded(hit.candidate),
                hit.rank,
                str(hit.candidate.canonical_id),
                str(hit.candidate.unit_id),
            ),
        )
        snippets: list[ContextSnippet] = []
        total_chars = 0
        total_tokens = 0
        omitted = 0
        status = STATUS_OK
        for hit in ordered_hits:
            candidate: RetrievalCandidate = hit.candidate
            key = f"{candidate.source_type}:{candidate.canonical_id}"
            if key in seen_sources:
                omitted += 1
                continue
            if freshest.get(key) is not candidate:
                omitted += 1
                continue
            seen_sources.add(key)
            if len(snippets) >= query.max_sources:
                omitted += 1
                continue
            snippet, truncated = query_snippet(candidate.content, query.query, max_chars=query.max_snippet_chars)
            normalized = normalize_text(snippet)
            if normalized in seen_texts:
                omitted += 1
                continue
            if total_chars + len(snippet) > query.max_total_chars:
                if snippets:
                    omitted += 1
                    continue
                snippet = snippet[: query.max_total_chars].rstrip()
                truncated = True
                status = STATUS_OVERSIZED
            snippet_tokens = estimate_tokens(snippet)
            if token_budget is not None and total_tokens + snippet_tokens > token_budget:
                if snippets:
                    omitted += 1
                    continue
                allowed_chars = max(token_budget * 4, 1)
                snippet = snippet[:allowed_chars].rstrip()
                snippet_tokens = estimate_tokens(snippet)
                truncated = True
                status = STATUS_OVERSIZED
            seen_texts.add(normalize_text(snippet))
            total_chars += len(snippet)
            total_tokens += snippet_tokens
            snippets.append(
                ContextSnippet(
                    rank=hit.rank,
                    title=candidate.title,
                    snippet=snippet,
                    truncated=truncated,
                    space_id=candidate.space_id,
                    canonical_id=str(candidate.canonical_id),
                    revision_id=str(candidate.revision_id),
                    citation_uri=candidate.citation_uri,
                    version=candidate.version,
                    source_type=candidate.source_type,
                    superseded=candidate_is_superseded(candidate),
                )
            )
        if not snippets:
            status = STATUS_NO_ANSWER
        omitted_reason = None
        if omitted:
            omitted_reason = f"{omitted} candidate(s) omitted by source-diversity and budget limits"
        abstained = result.explanation.abstained or not snippets
        return ContextPackage(
            query=result.query,
            snippets=tuple(snippets),
            total_chars=total_chars,
            budget_chars=query.max_total_chars,
            total_tokens=total_tokens,
            budget_tokens=token_budget,
            status=status if not abstained else STATUS_NO_ANSWER,
            omitted_count=omitted,
            omitted_reason=omitted_reason,
            abstained=abstained,
            degraded=result.health.semantic_status != "active",
        )

    async def _policy(self, ctx: UseCaseContext) -> EffectiveRuntimePolicy:
        if self._policy_loader is not None:
            return await self._policy_loader(ctx.principal)
        from src.gateway.infrastructure.runtime_settings_provider import load_active_runtime_policy

        return await load_active_runtime_policy(ctx.principal)


def context_package_response(package: ContextPackage) -> dict:
    return {
        "query": package.query,
        "snippets": [
            {
                "rank": snippet.rank,
                "title": snippet.title,
                "snippet": snippet.snippet,
                "truncated": snippet.truncated,
                "space_id": snippet.space_id,
                "canonical_id": snippet.canonical_id,
                "revision_id": snippet.revision_id,
                "citation_uri": snippet.citation_uri,
                "version": snippet.version,
                "source_type": snippet.source_type,
                "superseded": snippet.superseded,
            }
            for snippet in package.snippets
        ],
        "total_chars": package.total_chars,
        "budget_chars": package.budget_chars,
        "total_tokens": package.total_tokens,
        "budget_tokens": package.budget_tokens,
        "status": package.status,
        "omitted_count": package.omitted_count,
        "omitted_reason": package.omitted_reason,
        "abstained": package.abstained,
        "degraded": package.degraded,
        "estimation_method": package.estimation_method,
        "generated_at": package.generated_at,
    }
