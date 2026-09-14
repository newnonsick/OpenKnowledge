from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


MCP_PATH = "/mcp"
MCP_SERVER_NAME = "openknowledge"
MCP_PROTOCOL_VERSIONS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
    "2026-07-28",
)
MCP_HANDSHAKE_VERSIONS = MCP_PROTOCOL_VERSIONS[:4]
MCP_LATEST_HANDSHAKE_VERSION = MCP_HANDSHAKE_VERSIONS[-1]
MCP_MODERN_PROTOCOL_VERSION = MCP_PROTOCOL_VERSIONS[-1]

MCP_TOOL_NAMES = (
    "knowledge.search",
    "knowledge.fetch",
    "knowledge.create",
    "knowledge.update",
    "knowledge.archive",
    "context.assemble",
    "evidence.resolve",
    "ingestion.status",
    "ingestion.control",
)

MCP_PINNED_SPEC_VERSION = MCP_MODERN_PROTOCOL_VERSION

MCP_AUTH_SCOPES = (
    "knowledge:read",
    "knowledge:write",
)


class MCPSearchArguments(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    space_ids: list[str] | None = Field(default=None, max_length=100)
    active_space_id: str | None = Field(default=None, max_length=64)
    semantic_policy: Literal["prefer", "required", "disabled"] = "prefer"
    limit: int = Field(default=20, ge=1, le=50)
    tags: list[str] | None = Field(default=None, max_length=32)


class MCPFetchArguments(BaseModel):
    citation_uri: str | None = Field(default=None, min_length=1, max_length=1024)
    item_id: str | None = Field(default=None, max_length=64)
    revision_id: str | None = Field(default=None, max_length=64)
    document_id: str | None = Field(default=None, max_length=64)
    chunk_id: str | None = Field(default=None, max_length=64)


class MCPCreateArguments(BaseModel):
    space_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=1_000_000)
    tags: list[str] = Field(default_factory=list, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MCPUpdateArguments(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=1_000_000)
    tags: list[str] = Field(default_factory=list, max_length=32)
    change_summary: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MCPArchiveArguments(BaseModel):
    item_id: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class MCPContextArguments(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    space_ids: list[str] | None = Field(default=None, max_length=100)
    active_space_id: str | None = Field(default=None, max_length=64)
    semantic_policy: Literal["prefer", "required", "disabled"] = "prefer"
    max_sources: int = Field(default=8, ge=1, le=25)
    max_snippet_chars: int = Field(default=600, ge=100, le=4000)
    max_total_chars: int = Field(default=8000, ge=500, le=60000)
    tags: list[str] | None = Field(default=None, max_length=32)


class MCPResolveArguments(BaseModel):
    citation_uri: str = Field(min_length=1, max_length=1024)


class MCPIngestionStatusArguments(BaseModel):
    job_id: str | None = Field(default=None, max_length=64)
    space_id: str | None = Field(default=None, max_length=64)
    state: str | None = Field(default=None, max_length=32)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)


class MCPIngestionControlArguments(BaseModel):
    job_id: str = Field(min_length=1, max_length=64)
    operation: Literal["cancel", "retry"] = "cancel"
    idempotency_key: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True, slots=True)
class MCPCompatibilityEntry:
    protocol_version: str
    transport: str
    supported: bool
    session: bool = False
    negotiable_via_initialize: bool = True
    notes: str = ""


@dataclass(frozen=True, slots=True)
class MCPClientCompatibilityEntry:
    client: str
    transport: str
    protocol_version: str
    tested: bool = True
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "transport": self.transport,
            "protocol_version": self.protocol_version,
            "tested": self.tested,
            "notes": self.notes,
        }


MCP_TESTED_CLIENTS: tuple[MCPClientCompatibilityEntry, ...] = (
    MCPClientCompatibilityEntry(
        client="python-sdk-mcp==2.2.0",
        transport="streamable-http",
        protocol_version=MCP_PINNED_SPEC_VERSION,
        notes="ClientSession over streamable_http_client; initialize plus list_tools plus tools/call",
    ),
    MCPClientCompatibilityEntry(
        client="raw-http-streamable",
        transport="streamable-http",
        protocol_version=MCP_PINNED_SPEC_VERSION,
        notes="Raw httpx JSON-RPC 2.0 POST with Accept application/json plus text/event-stream; SSE frame decode",
    ),
)


@dataclass(frozen=True, slots=True)
class MCPVersionMatrix:
    server_name: str = MCP_SERVER_NAME
    sdk_version: str = ""
    latest_handshake_version: str = MCP_LATEST_HANDSHAKE_VERSION
    latest_modern_version: str = MCP_MODERN_PROTOCOL_VERSION
    pinned_spec_version: str = MCP_PINNED_SPEC_VERSION
    entries: tuple[MCPCompatibilityEntry, ...] = field(default_factory=tuple)
    clients: tuple[MCPClientCompatibilityEntry, ...] = MCP_TESTED_CLIENTS

    def as_dict(self) -> dict[str, Any]:
        return {
            "server": self.server_name,
            "sdk_version": self.sdk_version,
            "latest_handshake_version": self.latest_handshake_version,
            "latest_modern_version": self.latest_modern_version,
            "pinned_spec_version": self.pinned_spec_version,
            "protocols": [
                {
                    "protocol_version": entry.protocol_version,
                    "transport": entry.transport,
                    "supported": entry.supported,
                    "session": entry.session,
                    "negotiable_via_initialize": entry.negotiable_via_initialize,
                    "notes": entry.notes,
                }
                for entry in self.entries
            ],
            "clients": [entry.as_dict() for entry in self.clients],
        }


def default_version_matrix(sdk_version: str = "", *, supported_versions: tuple[str, ...] | None = None) -> MCPVersionMatrix:
    supported = set(supported_versions) if supported_versions is not None else set(MCP_PROTOCOL_VERSIONS)
    entries = tuple(
        MCPCompatibilityEntry(
            protocol_version=version,
            transport="streamable-http",
            supported=version in supported,
            session=False,
            negotiable_via_initialize=version in MCP_HANDSHAKE_VERSIONS,
            notes=(
                "modern per-request envelope; not negotiable via initialize"
                if version == MCP_MODERN_PROTOCOL_VERSION
                else "handshake wire, stateless: no session tracking or resumption"
            ),
        )
        for version in MCP_PROTOCOL_VERSIONS
    )
    return MCPVersionMatrix(sdk_version=sdk_version, entries=entries)


def discovery_document(
    *,
    base_url: str,
    version_matrix: MCPVersionMatrix | None = None,
    oidc_issuer: str | None = None,
    oidc_audience: str | None = None,
) -> dict[str, Any]:
    normalized = base_url.rstrip("/")
    matrix = version_matrix or default_version_matrix()
    schemes = ["personal_api_key"]
    if oidc_issuer:
        schemes.append("oidc_bearer")
    return {
        "server": MCP_SERVER_NAME,
        "endpoint": f"{normalized}{MCP_PATH}",
        "transports": ["streamable-http"],
        "tools": list(MCP_TOOL_NAMES),
        "auth": {
            "schemes": schemes,
            "header": "Authorization: Bearer <personal-api-key>",
            "alternative_header": "X-API-Key: <personal-api-key>",
            "discovery": f"{normalized}/.well-known/oauth-protected-resource{MCP_PATH}",
            "scopes_supported": list(MCP_AUTH_SCOPES),
            "flows": {
                "personal_api_key_bridge": {
                    "use": "private and local harnesses",
                    "header": "Authorization: Bearer <personal-api-key>",
                    "alternative_header": "X-API-Key: <personal-api-key>",
                },
                "oidc_bearer": {
                    "use": "remote and SSO harnesses",
                    "enabled": bool(oidc_issuer),
                    "issuer": oidc_issuer,
                    "audience": oidc_audience,
                    "header": "Authorization: Bearer <oidc-access-token>",
                },
            },
        },
        "protocol_versions": list(MCP_PROTOCOL_VERSIONS),
        "latest_handshake_version": MCP_LATEST_HANDSHAKE_VERSION,
        "latest_modern_version": MCP_MODERN_PROTOCOL_VERSION,
        "compatibility": matrix.as_dict(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_uuid(value: str, *, field_name: str) -> UUID:
    from src.gateway.domain.exceptions import ValidationException

    try:
        return UUID(value)
    except ValueError as exc:
        raise ValidationException(f"Invalid {field_name}.") from exc
