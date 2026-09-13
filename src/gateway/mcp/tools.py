from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.gateway.application.services.context_assembly_service import (
    AssembleContextQuery,
    ContextAssembler,
    context_package_response,
)
from src.gateway.application.services.evidence_service import (
    EvidenceReference,
    EvidenceService,
    evidence_response,
    parse_citation_uri,
)
from src.gateway.application.services.permission_service import require_profile_route
from src.gateway.application.services.quota_service import QuotaGuard
from src.gateway.application.services.runtime_settings_service import EffectiveRuntimePolicy
from src.gateway.application.use_cases import (
    CreateKnowledgeCommand,
    DeleteKnowledgeCommand,
    GetIngestionJobQuery,
    IngestionUseCases,
    KnowledgeCommands,
    ListIngestionJobsQuery,
    MutateIngestionJobCommand,
    RetrievalQueries,
    SearchKnowledgeQuery,
    UpdateKnowledgeCommand,
    UseCaseContext,
)
from src.gateway.domain.exceptions import AuthorizationException, ValidationException
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.database import get_session_factory
from src.gateway.infrastructure.runtime_settings_provider import load_active_runtime_policy
from src.gateway.mcp.auth import MCPRequestAuthenticator, mcp_quota_guard, require_mcp_scope
from src.gateway.mcp.contracts import (
    MCPArchiveArguments,
    MCPContextArguments,
    MCPCreateArguments,
    MCPFetchArguments,
    MCPIngestionControlArguments,
    MCPIngestionStatusArguments,
    MCPResolveArguments,
    MCPSearchArguments,
    MCPUpdateArguments,
    parse_uuid,
)


class MCPToolRuntime:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        authenticator: MCPRequestAuthenticator | None = None,
        policy_loader=None,
    ) -> None:
        self._session_factory = session_factory
        self._authenticator = authenticator or MCPRequestAuthenticator(session_factory=session_factory)
        self._policy_loader = policy_loader or load_active_runtime_policy

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is not None:
            return self._session_factory
        return get_session_factory()

    async def _principal(self, headers: Mapping[str, str]) -> Principal:
        return await self._authenticator.authenticate(headers)

    async def _policy(self, principal: Principal) -> EffectiveRuntimePolicy:
        return await self._policy_loader(principal)

    def _context(
        self,
        principal: Principal,
        policy: EffectiveRuntimePolicy | None,
        headers: Mapping[str, str],
        *,
        idempotency_key: str | None = None,
    ) -> UseCaseContext:
        key = idempotency_key if idempotency_key else MCPRequestAuthenticator.idempotency_key(headers)
        return UseCaseContext(
            principal=principal,
            policy=policy,
            request_id=MCPRequestAuthenticator.request_id(headers),
            idempotency_key=key,
        )

    def _guard(self, principal: Principal, space_id: str | None) -> QuotaGuard:
        return mcp_quota_guard(principal, space_id)

    async def knowledge_search(self, args: MCPSearchArguments, headers: Mapping[str, str]) -> dict[str, Any]:
        principal = await require_mcp_scope(await self._principal(headers), "knowledge:read")
        require_profile_route(principal, "knowledge.search")
        policy = await self._policy(principal)
        async with self._guard(principal, args.active_space_id or (args.space_ids[0] if args.space_ids else None)):
            return await RetrievalQueries().search(
                self._context(principal, policy, headers),
                SearchKnowledgeQuery(
                    query=args.query,
                    space_ids=tuple(args.space_ids) if args.space_ids is not None else None,
                    active_space_id=args.active_space_id,
                    semantic_policy=args.semantic_policy,
                    limit=args.limit,
                    tags=tuple(args.tags) if args.tags is not None else None,
                ),
            )

    async def knowledge_fetch(self, args: MCPFetchArguments, headers: Mapping[str, str]) -> dict[str, Any]:
        principal = await require_mcp_scope(await self._principal(headers), "knowledge:read")
        require_profile_route(principal, "knowledge.read")
        reference = self._fetch_reference(args)
        factory = self._factory()
        async with self._guard(principal, reference.space_id or None):
            async with factory() as session:
                resolved = await resolve_fetch_space(session, reference)
                ctx = self._context(principal, None, headers)
                payload = await EvidenceService(session).fetch(ctx, resolved)
                await session.commit()
        return evidence_response(payload)

    async def knowledge_create(self, args: MCPCreateArguments, headers: Mapping[str, str]) -> dict[str, Any]:
        principal = await require_mcp_scope(await self._principal(headers), "knowledge:write")
        require_profile_route(principal, "knowledge.create")
        policy = await self._policy(principal)
        factory = self._factory()
        async with self._guard(principal, args.space_id):
            async with factory() as session:
                outcome = await KnowledgeCommands(session).create(
                    self._context(principal, policy, headers, idempotency_key=args.idempotency_key),
                    CreateKnowledgeCommand(
                        space_id=args.space_id,
                        title=args.title,
                        content=args.content,
                        tags=tuple(args.tags),
                    ),
                )
                assert outcome.value is not None
                payload = self._knowledge_payload(outcome.value)
                await session.commit()
        payload["replayed"] = outcome.replayed
        return payload

    async def knowledge_update(self, args: MCPUpdateArguments, headers: Mapping[str, str]) -> dict[str, Any]:
        principal = await require_mcp_scope(await self._principal(headers), "knowledge:write")
        require_profile_route(principal, "knowledge.update")
        policy = await self._policy(principal)
        item_id = parse_uuid(args.item_id, field_name="item_id")
        factory = self._factory()
        async with self._guard(principal, None):
            async with factory() as session:
                item = await KnowledgeCommands(session).get(self._context(principal, None, headers), item_id)
                space_id = item.workspace_id
                await session.commit()
            async with factory() as session:
                outcome = await KnowledgeCommands(session).update(
                    self._context(principal, policy, headers, idempotency_key=args.idempotency_key),
                    UpdateKnowledgeCommand(
                        item_id=item_id,
                        expected_version=args.expected_version,
                        title=args.title,
                        content=args.content,
                        tags=tuple(args.tags),
                        change_summary=args.change_summary,
                    ),
                )
                assert outcome.value is not None
                payload = self._knowledge_payload(outcome.value)
                await session.commit()
        payload["replayed"] = outcome.replayed
        return payload

    async def knowledge_archive(self, args: MCPArchiveArguments, headers: Mapping[str, str]) -> dict[str, Any]:
        principal = await require_mcp_scope(await self._principal(headers), "knowledge:write")
        require_profile_route(principal, "knowledge.delete")
        policy = await self._policy(principal)
        item_id = parse_uuid(args.item_id, field_name="item_id")
        factory = self._factory()
        async with self._guard(principal, None):
            async with factory() as session:
                outcome = await KnowledgeCommands(session).delete(
                    self._context(principal, policy, headers, idempotency_key=args.idempotency_key),
                    DeleteKnowledgeCommand(item_id=item_id, expected_version=args.expected_version),
                )
                await session.commit()
        return {"id": str(item_id), "archived": True, "replayed": outcome.replayed}

    async def context_assemble(self, args: MCPContextArguments, headers: Mapping[str, str]) -> dict[str, Any]:
        principal = await require_mcp_scope(await self._principal(headers), "knowledge:read")
        require_profile_route(principal, "knowledge.search")
        policy = await self._policy(principal)
        async with self._guard(principal, args.active_space_id or (args.space_ids[0] if args.space_ids else None)):
            package = await ContextAssembler().assemble(
                self._context(principal, policy, headers),
                AssembleContextQuery(
                    query=args.query,
                    space_ids=tuple(args.space_ids) if args.space_ids is not None else None,
                    active_space_id=args.active_space_id,
                    semantic_policy=args.semantic_policy,
                    max_sources=args.max_sources,
                    max_snippet_chars=args.max_snippet_chars,
                    max_total_chars=args.max_total_chars,
                    tags=tuple(args.tags) if args.tags is not None else None,
                ),
            )
        return context_package_response(package)

    async def evidence_resolve(self, args: MCPResolveArguments, headers: Mapping[str, str]) -> dict[str, Any]:
        principal = await require_mcp_scope(await self._principal(headers), "knowledge:read")
        require_profile_route(principal, "knowledge.read")
        reference = parse_citation_uri(args.citation_uri)
        factory = self._factory()
        async with self._guard(principal, reference.space_id):
            async with factory() as session:
                payload = await EvidenceService(session).resolve(self._context(principal, None, headers), args.citation_uri)
                await session.commit()
        return evidence_response(payload)

    async def ingestion_status(self, args: MCPIngestionStatusArguments, headers: Mapping[str, str]) -> dict[str, Any]:
        principal = await require_mcp_scope(await self._principal(headers), "knowledge:read")
        require_profile_route(principal, "ingestion_job.list")
        factory = self._factory()
        ctx = self._context(principal, None, headers)
        if args.job_id is not None:
            job_id = parse_uuid(args.job_id, field_name="job_id")
            async with self._guard(principal, None):
                async with factory() as session:
                    payload = await IngestionUseCases(session).get_job(ctx, GetIngestionJobQuery(job_id=job_id))
                    await session.commit()
            return {"job": payload}
        async with self._guard(principal, args.space_id):
            async with factory() as session:
                jobs, metadata = await IngestionUseCases(session).list_jobs(
                    ctx,
                    ListIngestionJobsQuery(
                        space_id=args.space_id,
                        state=args.state,
                        page=args.page,
                        page_size=args.page_size,
                    ),
                )
                await session.commit()
        return {"items": jobs, **metadata}

    async def ingestion_control(self, args: MCPIngestionControlArguments, headers: Mapping[str, str]) -> dict[str, Any]:
        principal = await require_mcp_scope(await self._principal(headers), "knowledge:write")
        require_profile_route(principal, "ingestion_job.mutate")
        policy = await self._policy(principal)
        job_id = parse_uuid(args.job_id, field_name="job_id")
        factory = self._factory()
        async with self._guard(principal, None):
            async with factory() as session:
                outcome = await IngestionUseCases(session).mutate_job(
                    self._context(principal, policy, headers, idempotency_key=args.idempotency_key),
                    MutateIngestionJobCommand(job_id=job_id, operation=args.operation),
                )
                await session.commit()
        assert outcome.value is not None
        return {"job": outcome.value, "replayed": outcome.replayed}

    @staticmethod
    def _fetch_reference(args: MCPFetchArguments) -> EvidenceReference:
        if args.citation_uri is not None:
            return parse_citation_uri(args.citation_uri)
        if args.item_id is not None and args.revision_id is not None:
            return EvidenceReference(
                "knowledge_revision",
                "",
                parse_uuid(args.item_id, field_name="item_id"),
                parse_uuid(args.revision_id, field_name="revision_id"),
            )
        if args.document_id is not None and args.revision_id is not None:
            chunk_id = parse_uuid(args.chunk_id, field_name="chunk_id") if args.chunk_id is not None else None
            return EvidenceReference(
                "document_chunk",
                "",
                parse_uuid(args.document_id, field_name="document_id"),
                parse_uuid(args.revision_id, field_name="revision_id"),
                chunk_id,
            )
        raise ValidationException("A citation URI or explicit revision identifiers are required.")

    @staticmethod
    def _knowledge_payload(item) -> dict[str, Any]:
        revision = item.current_revision
        content = revision.content if revision else item.content or ""
        version = revision.version if revision else item.version
        tags = list(revision.tags if revision else item.tags)
        return {
            "id": str(item.id),
            "space_id": item.workspace_id,
            "title": item.title,
            "content": content,
            "content_excerpt": content[:320],
            "tags": tags,
            "version": version,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }


async def resolve_fetch_space(
    session: AsyncSession,
    reference: EvidenceReference,
) -> EvidenceReference:
    if reference.space_id:
        return reference
    if reference.kind == "knowledge_revision":
        from src.gateway.infrastructure.persistence.models import KnowledgeItem as KnowledgeItemModel

        item = await session.get(KnowledgeItemModel, reference.canonical_id)
        if item is None:
            raise AuthorizationException()
        return EvidenceReference(
            reference.kind, item.workspace_id, reference.canonical_id, reference.revision_id, reference.chunk_id
        )
    from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel

    document = await session.get(DocumentModel, reference.canonical_id)
    if document is None:
        raise AuthorizationException()
    return EvidenceReference(
        reference.kind, document.space_id, reference.canonical_id, reference.revision_id, reference.chunk_id
    )
