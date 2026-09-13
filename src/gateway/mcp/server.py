from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from src.gateway.domain.exceptions import GatewayException, QuotaExceededException
from src.gateway.mcp.auth import quota_error_details
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
    MCP_PROTOCOL_VERSIONS,
    MCP_SERVER_NAME,
)
from src.gateway.mcp.tools import MCPToolRuntime

try:
    from mcp.types.version import SUPPORTED_PROTOCOL_VERSIONS as _SDK_PROTOCOL_VERSIONS
except ImportError:
    try:
        from mcp_types.version import SUPPORTED_PROTOCOL_VERSIONS as _SDK_PROTOCOL_VERSIONS
    except ImportError:
        _SDK_PROTOCOL_VERSIONS = ()


class SearchOutput(BaseModel):
    query: str
    hits: list[dict[str, Any]] = Field(default_factory=list)
    health: dict[str, Any] = Field(default_factory=dict)
    explanation: dict[str, Any] = Field(default_factory=dict)


class FetchOutput(BaseModel):
    kind: str
    space_id: str
    canonical_id: str
    revision_id: str
    chunk_id: str | None = None
    title: str
    content: str
    version: int | None = None
    superseded: bool = False
    citation_uri: str


class KnowledgeMutationOutput(BaseModel):
    id: str
    space_id: str
    title: str
    content: str
    content_excerpt: str
    tags: list[str] = Field(default_factory=list)
    version: int
    created_at: str
    updated_at: str
    replayed: bool = False


class ArchiveOutput(BaseModel):
    id: str
    archived: bool = True
    replayed: bool = False


class ContextOutput(BaseModel):
    query: str
    snippets: list[dict[str, Any]] = Field(default_factory=list)
    total_chars: int = 0
    budget_chars: int = 0
    omitted_count: int = 0
    omitted_reason: str | None = None
    abstained: bool = False
    degraded: bool = False
    estimation_method: str = ""
    generated_at: str = ""


class IngestionStatusOutput(BaseModel):
    job: dict[str, Any] | None = None
    items: list[dict[str, Any]] | None = None
    page: int | None = None
    page_size: int | None = None
    total_items: int | None = None
    total_pages: int | None = None


class IngestionControlOutput(BaseModel):
    job: dict[str, Any]
    replayed: bool = False


def supported_protocol_versions() -> tuple[str, ...]:
    if _SDK_PROTOCOL_VERSIONS:
        return tuple(_SDK_PROTOCOL_VERSIONS)
    return MCP_PROTOCOL_VERSIONS


def _headers(ctx: Context | None) -> Mapping[str, str]:
    if ctx is None:
        return {}
    try:
        headers = ctx.headers
    except (AttributeError, RuntimeError):
        return {}
    return dict(headers or {})


def _raise_mcp_error(exc: Exception) -> None:
    if isinstance(exc, QuotaExceededException):
        details = quota_error_details(exc)
        raise ToolError(f"quota_exceeded: retry after {details['retry_after_seconds']}s") from exc
    if isinstance(exc, GatewayException):
        raise ToolError(f"{exc.code}: {exc.message}") from exc
    logger = logging.getLogger(__name__)
    logger.error("Unhandled MCP tool error", extra={"exception_class": type(exc).__name__})
    raise ToolError("internal_error: An internal server error occurred.") from exc


def build_mcp_server(runtime: MCPToolRuntime | None = None) -> MCPServer:
    active = runtime or MCPToolRuntime()
    server = MCPServer(name=MCP_SERVER_NAME)

    @server.tool(name="knowledge.search", description="Search workspace knowledge with hybrid retrieval and citations.")
    async def knowledge_search(
        query: str,
        ctx: Context,
        space_ids: list[str] | None = None,
        active_space_id: str | None = None,
        semantic_policy: str = "prefer",
        limit: int = 20,
        tags: list[str] | None = None,
    ) -> SearchOutput:
        try:
            payload = await active.knowledge_search(
                MCPSearchArguments(
                    query=query,
                    space_ids=space_ids,
                    active_space_id=active_space_id,
                    semantic_policy=semantic_policy,  # type: ignore[arg-type]
                    limit=limit,
                    tags=tags,
                ),
                _headers(ctx),
            )
        except Exception as exc:
            _raise_mcp_error(exc)
            raise
        return SearchOutput(
            query=str(payload.get("query", query)),
            hits=list(payload.get("hits", [])),
            health=dict(payload.get("health", {})),
            explanation=dict(payload.get("explanation", {})),
        )

    @server.tool(name="knowledge.fetch", description="Fetch an exact knowledge or document revision by citation URI or IDs.")
    async def knowledge_fetch(
        ctx: Context,
        citation_uri: str | None = None,
        item_id: str | None = None,
        revision_id: str | None = None,
        document_id: str | None = None,
        chunk_id: str | None = None,
    ) -> FetchOutput:
        try:
            payload = await active.knowledge_fetch(
                MCPFetchArguments(
                    citation_uri=citation_uri,
                    item_id=item_id,
                    revision_id=revision_id,
                    document_id=document_id,
                    chunk_id=chunk_id,
                ),
                _headers(ctx),
            )
        except Exception as exc:
            _raise_mcp_error(exc)
            raise
        return FetchOutput(
            kind=str(payload["kind"]),
            space_id=str(payload["space_id"]),
            canonical_id=str(payload["canonical_id"]),
            revision_id=str(payload["revision_id"]),
            chunk_id=payload.get("chunk_id"),
            title=str(payload.get("title", "")),
            content=str(payload.get("content", "")),
            version=payload.get("version"),
            superseded=bool(payload.get("superseded", False)),
            citation_uri=str(payload.get("citation_uri", "")),
        )

    @server.tool(name="knowledge.create", description="Create a knowledge item in a space. Requires an idempotency key.")
    async def knowledge_create(
        space_id: str,
        title: str,
        content: str,
        ctx: Context,
        tags: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> KnowledgeMutationOutput:
        try:
            payload = await active.knowledge_create(
                MCPCreateArguments(
                    space_id=space_id,
                    title=title,
                    content=content,
                    tags=tags or [],
                    idempotency_key=idempotency_key or "",
                ),
                _headers(ctx),
            )
        except Exception as exc:
            _raise_mcp_error(exc)
            raise
        return KnowledgeMutationOutput(
            id=str(payload["id"]),
            space_id=str(payload["space_id"]),
            title=str(payload["title"]),
            content=str(payload.get("content", "")),
            content_excerpt=str(payload.get("content_excerpt", "")),
            tags=list(payload.get("tags", [])),
            version=int(payload.get("version", 1)),
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            replayed=bool(payload.get("replayed", False)),
        )

    @server.tool(name="knowledge.update", description="Update a knowledge item with optimistic concurrency. Requires an idempotency key.")
    async def knowledge_update(
        item_id: str,
        expected_version: int,
        title: str,
        content: str,
        ctx: Context,
        tags: list[str] | None = None,
        change_summary: str | None = None,
        idempotency_key: str | None = None,
    ) -> KnowledgeMutationOutput:
        try:
            payload = await active.knowledge_update(
                MCPUpdateArguments(
                    item_id=item_id,
                    expected_version=expected_version,
                    title=title,
                    content=content,
                    tags=tags or [],
                    change_summary=change_summary,
                    idempotency_key=idempotency_key or "",
                ),
                _headers(ctx),
            )
        except Exception as exc:
            _raise_mcp_error(exc)
            raise
        return KnowledgeMutationOutput(
            id=str(payload["id"]),
            space_id=str(payload["space_id"]),
            title=str(payload["title"]),
            content=str(payload.get("content", "")),
            content_excerpt=str(payload.get("content_excerpt", "")),
            tags=list(payload.get("tags", [])),
            version=int(payload.get("version", 1)),
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            replayed=bool(payload.get("replayed", False)),
        )

    @server.tool(name="knowledge.archive", description="Archive a knowledge item with optimistic concurrency. Requires an idempotency key.")
    async def knowledge_archive(
        item_id: str,
        expected_version: int,
        ctx: Context,
        idempotency_key: str | None = None,
    ) -> ArchiveOutput:
        try:
            payload = await active.knowledge_archive(
                MCPArchiveArguments(
                    item_id=item_id,
                    expected_version=expected_version,
                    idempotency_key=idempotency_key or "",
                ),
                _headers(ctx),
            )
        except Exception as exc:
            _raise_mcp_error(exc)
            raise
        return ArchiveOutput(id=str(payload["id"]), archived=True, replayed=bool(payload.get("replayed", False)))

    @server.tool(name="context.assemble", description="Assemble a budgeted context package with citations for a query.")
    async def context_assemble(
        query: str,
        ctx: Context,
        space_ids: list[str] | None = None,
        active_space_id: str | None = None,
        semantic_policy: str = "prefer",
        max_sources: int = 8,
        max_snippet_chars: int = 600,
        max_total_chars: int = 8000,
        tags: list[str] | None = None,
    ) -> ContextOutput:
        try:
            payload = await active.context_assemble(
                MCPContextArguments(
                    query=query,
                    space_ids=space_ids,
                    active_space_id=active_space_id,
                    semantic_policy=semantic_policy,  # type: ignore[arg-type]
                    max_sources=max_sources,
                    max_snippet_chars=max_snippet_chars,
                    max_total_chars=max_total_chars,
                    tags=tags,
                ),
                _headers(ctx),
            )
        except Exception as exc:
            _raise_mcp_error(exc)
            raise
        return ContextOutput(
            query=str(payload.get("query", query)),
            snippets=list(payload.get("snippets", [])),
            total_chars=int(payload.get("total_chars", 0)),
            budget_chars=int(payload.get("budget_chars", 0)),
            omitted_count=int(payload.get("omitted_count", 0)),
            omitted_reason=payload.get("omitted_reason"),
            abstained=bool(payload.get("abstained", False)),
            degraded=bool(payload.get("degraded", False)),
            estimation_method=str(payload.get("estimation_method", "")),
            generated_at=str(payload.get("generated_at", "")),
        )

    @server.tool(name="evidence.resolve", description="Resolve a citation URI to its exact revision content.")
    async def evidence_resolve(citation_uri: str, ctx: Context) -> FetchOutput:
        try:
            payload = await active.evidence_resolve(MCPResolveArguments(citation_uri=citation_uri), _headers(ctx))
        except Exception as exc:
            _raise_mcp_error(exc)
            raise
        return FetchOutput(
            kind=str(payload["kind"]),
            space_id=str(payload["space_id"]),
            canonical_id=str(payload["canonical_id"]),
            revision_id=str(payload["revision_id"]),
            chunk_id=payload.get("chunk_id"),
            title=str(payload.get("title", "")),
            content=str(payload.get("content", "")),
            version=payload.get("version"),
            superseded=bool(payload.get("superseded", False)),
            citation_uri=str(payload.get("citation_uri", "")),
        )

    @server.tool(name="ingestion.status", description="Inspect an ingestion job or list jobs in a space.")
    async def ingestion_status(
        ctx: Context,
        job_id: str | None = None,
        space_id: str | None = None,
        state: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> IngestionStatusOutput:
        try:
            payload = await active.ingestion_status(
                MCPIngestionStatusArguments(
                    job_id=job_id,
                    space_id=space_id,
                    state=state,
                    page=page,
                    page_size=page_size,
                ),
                _headers(ctx),
            )
        except Exception as exc:
            _raise_mcp_error(exc)
            raise
        job = payload.get("job")
        items = payload.get("items")
        return IngestionStatusOutput(
            job=dict(job) if isinstance(job, dict) else None,
            items=list(items) if isinstance(items, list) else None,
            page=payload.get("page"),
            page_size=payload.get("page_size"),
            total_items=payload.get("total_items"),
            total_pages=payload.get("total_pages"),
        )

    @server.tool(name="ingestion.control", description="Request ingestion job cancellation or retry. Requires an idempotency key.")
    async def ingestion_control(
        job_id: str,
        ctx: Context,
        operation: str = "cancel",
        idempotency_key: str | None = None,
    ) -> IngestionControlOutput:
        try:
            payload = await active.ingestion_control(
                MCPIngestionControlArguments(
                    job_id=job_id,
                    operation=operation,  # type: ignore[arg-type]
                    idempotency_key=idempotency_key or "",
                ),
                _headers(ctx),
            )
        except Exception as exc:
            _raise_mcp_error(exc)
            raise
        job = payload.get("job")
        assert isinstance(job, dict)
        return IngestionControlOutput(job=job, replayed=bool(payload.get("replayed", False)))

    return server


def version_matrix_payload() -> dict[str, Any]:
    import mcp

    from src.gateway.mcp.contracts import default_version_matrix

    matrix = default_version_matrix(
        sdk_version=getattr(mcp, "__version__", ""),
        supported_versions=supported_protocol_versions(),
    )
    return matrix.as_dict()


def discovery_payload(base_url: str) -> dict[str, Any]:
    from src.gateway.mcp.contracts import discovery_document

    normalized = base_url.rstrip("/")
    return discovery_document(base_url=normalized)
