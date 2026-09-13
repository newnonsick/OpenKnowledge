from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings

from src.gateway.config import get_settings
from src.gateway.mcp.contracts import MCP_PATH
from src.gateway.mcp.server import build_mcp_server, discovery_payload, version_matrix_payload
from src.gateway.mcp.tools import MCPToolRuntime


router = APIRouter(tags=["mcp"])
logger = logging.getLogger(__name__)


def build_mcp_security(trusted_hosts: list[str] | tuple[str, ...] | None) -> TransportSecuritySettings:
    hosts = [str(host).strip() for host in (trusted_hosts or []) if str(host).strip()]
    if not hosts or "*" in hosts:
        logger.warning("MCP DNS rebinding protection disabled: explicit trusted hosts are not configured")
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    patterns = list(hosts)
    for host in hosts:
        if ":" not in host:
            patterns.append(f"{host}:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=patterns,
        allowed_origins=[],
    )


def build_mcp_components(
    *,
    runtime: MCPToolRuntime | None = None,
    trusted_hosts: list[str] | tuple[str, ...] | None = None,
):
    server = build_mcp_server(runtime or MCPToolRuntime())
    sub_app = server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=build_mcp_security(trusted_hosts),
    )
    return server, sub_app


@router.get("/mcp/versions")
async def mcp_versions() -> dict[str, Any]:
    return version_matrix_payload()


@router.get("/mcp/discovery")
async def mcp_discovery(request: Request) -> dict[str, Any]:
    settings = get_settings()
    base_url = str(settings.gateway.public_base_url).rstrip("/")
    return discovery_payload(base_url or str(request.base_url).rstrip("/"))


@router.get("/.well-known/oauth-protected-resource/mcp")
async def mcp_protected_resource(request: Request) -> JSONResponse:
    settings = get_settings()
    base_url = str(settings.gateway.public_base_url).rstrip("/") or str(request.base_url).rstrip("/")
    metadata = {
        "resource": f"{base_url}{MCP_PATH}/",
        "authorization_servers": [],
        "scopes_supported": ["knowledge:read", "knowledge:write"],
        "bearer_methods_supported": ["header"],
    }
    response = JSONResponse(content=metadata)
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response
