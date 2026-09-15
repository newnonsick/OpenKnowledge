from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services import oidc_service
from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings, reset_runtime_settings, set_runtime_settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, Workspace
from src.gateway.main import create_app
from src.gateway.mcp import auth as mcp_auth_module
from src.gateway.mcp.contracts import MCP_MODERN_PROTOCOL_VERSION
from src.gateway.mcp.server import build_mcp_server, version_matrix_payload
from tests.integration.postgres_test_database import isolated_postgres_database


PEPPER = "test-mcp-pepper-with-adequate-length"


def _member_principal(member_id) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
    )


def _settings() -> Settings:
    return Settings(
        gateway={
            "environment": "test",
            "public_base_url": "https://gateway.test",
            "api_key_peppers": {1: PEPPER},
            "active_api_key_pepper_version": 1,
            "trusted_hosts": ["testserver", "gateway.test"],
        }
    )


async def _provision(*, space_grants: set[str] | None = None) -> tuple:
    now = datetime.now(timezone.utc)
    codec = APIKeyCodec({1: SecretValue(PEPPER)}, active_pepper_version=1)
    async with isolated_postgres_database() as (_, factory):
        member_a = uuid4()
        member_b = uuid4()
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_a,
                    username="mcp-owner",
                    username_normalized="mcp-owner",
                    display_name="MCP Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(
                MemberModel(
                    id=member_b,
                    username="mcp-other",
                    username_normalized="mcp-other",
                    display_name="MCP Other",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="space-a", name="Space A", created_by_member_id=member_a))
            session.add(Workspace(id="space-b", name="Space B", created_by_member_id=member_a))
            session.add(
                EmbeddingGenerationModel(
                    id=uuid4(),
                    purpose="retrieval",
                    model_id="mcp-test-generation",
                    dimensions=EMBED_DIM,
                    status="active",
                )
            )
            await session.flush()
            session.add_all(
                [
                    SpaceMembershipModel(id=uuid4(), space_id="space-a", member_id=member_a, role="owner"),
                    SpaceMembershipModel(id=uuid4(), space_id="space-b", member_id=member_a, role="owner"),
                ]
            )
        async with factory.begin() as session:
            website_session = await SessionService(session).issue(
                _member_principal(member_a), now=now, step_up_at=now
            )
            kwargs: dict = {}
            if space_grants is not None:
                kwargs["space_grants"] = space_grants
            created = await APIKeyService(session, codec).create(
                member_a,
                family_id=website_session.family_id,
                name="MCP key",
                scopes={"knowledge:read", "knowledge:write"},
                request_id="mcp-key-create",
                now=now,
                **kwargs,
            )
            raw_key = created.secret.reveal()
        app = create_app(_settings())
        set_session_factory(factory)
        try:
            yield app, factory, raw_key, member_a
        finally:
            set_session_factory(None)


@pytest.fixture()
async def mcp_provisioned():
    async for value in _provision():
        yield value


@pytest.fixture()
async def mcp_provisioned_narrow():
    async for value in _provision(space_grants={"space-a"}):
        yield value


def _rpc(mid: int, method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}


def _wire_result(response: httpx.Response) -> dict:
    assert response.status_code == 200, response.text[:300]
    frames = [
        line[len("data: "):].strip()
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert frames, response.text[:300]
    return json.loads(frames[-1])


async def _initialize(client: httpx.AsyncClient, protocol_version: str = "2025-06-18") -> None:
    response = await client.post(
        "/mcp/",
        json=_rpc(
            1,
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "mcp-test", "version": "0"},
            },
        ),
        headers={"Accept": "application/json, text/event-stream"},
    )
    payload = _wire_result(response)
    assert payload["result"]["protocolVersion"] == protocol_version


async def _call_tool(
    client: httpx.AsyncClient, mid: int, name: str, arguments: dict, *, api_key: str | None = None
) -> dict:
    headers = {"Accept": "application/json, text/event-stream"}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"
    response = await client.post("/mcp/", json=_rpc(mid, "tools/call", {"name": name, "arguments": arguments}), headers=headers)
    return _wire_result(response)["result"]


EXPECTED_MCP_TOOLS = [
    "knowledge.search",
    "knowledge.fetch",
    "knowledge.create",
    "knowledge.update",
    "knowledge.archive",
    "context.assemble",
    "evidence.resolve",
    "ingestion.status",
    "ingestion.control",
]


def test_mcp_tool_catalog_matches_contract() -> None:
    from src.gateway.mcp.contracts import MCP_TOOL_NAMES

    server = build_mcp_server()
    assert [tool.name for tool in server._tool_manager.list_tools()] == EXPECTED_MCP_TOOLS
    assert list(MCP_TOOL_NAMES) == EXPECTED_MCP_TOOLS


def test_mcp_version_matrix_lists_all_protocols() -> None:
    matrix = version_matrix_payload()
    assert matrix["latest_handshake_version"] == "2025-11-25"
    assert matrix["latest_modern_version"] == "2026-07-28"
    assert [entry["protocol_version"] for entry in matrix["protocols"]] == ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]
    assert all(entry["supported"] for entry in matrix["protocols"])
    assert all(entry["transport"] == "streamable-http" for entry in matrix["protocols"])
    assert all(entry["session"] is False for entry in matrix["protocols"])
    modern = [entry for entry in matrix["protocols"] if entry["protocol_version"] == MCP_MODERN_PROTOCOL_VERSION][0]
    assert modern["negotiable_via_initialize"] is False
    for entry in matrix["protocols"]:
        if entry["protocol_version"] != MCP_MODERN_PROTOCOL_VERSION:
            assert entry["negotiable_via_initialize"] is True


async def test_mcp_versions_and_discovery_endpoints(mcp_provisioned) -> None:
    app, _, _, _ = mcp_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            versions = await client.get("/mcp/versions")
            assert versions.status_code == 200
            assert [entry["protocol_version"] for entry in versions.json()["protocols"]] == ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"]
            discovery = await client.get("/mcp/discovery")
            assert discovery.status_code == 200
            assert discovery.json()["endpoint"] == "https://gateway.test/mcp"
            assert discovery.json()["tools"] == EXPECTED_MCP_TOOLS
            protected = await client.get("/.well-known/oauth-protected-resource/mcp")
            assert protected.status_code == 200
            assert protected.json()["resource"] == "https://gateway.test/mcp/"


@pytest.mark.parametrize("protocol_version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
async def test_mcp_handshake_versions_negotiate(mcp_provisioned, protocol_version: str) -> None:
    app, _, _, _ = mcp_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client, protocol_version)


async def test_mcp_unsupported_version_falls_back(mcp_provisioned) -> None:
    app, _, _, _ = mcp_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/mcp/",
                json=_rpc(
                    1,
                    "initialize",
                    {"protocolVersion": "1999-01-01", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
                ),
                headers={"Accept": "application/json, text/event-stream"},
            )
            payload = _wire_result(response)
            assert payload["result"]["protocolVersion"] == "2025-11-25"


async def test_mcp_modern_wire_single_exchange(mcp_provisioned) -> None:
    app, _, _, _ = mcp_provisioned
    meta = {
        "io.modelcontextprotocol/protocolVersion": MCP_MODERN_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "mcp-protocol-version": MCP_MODERN_PROTOCOL_VERSION,
        "mcp-method": "tools/list",
    }
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/mcp/", json=_rpc(2, "tools/list", {"_meta": meta}), headers=headers
            )
            assert response.status_code == 200
            payload = json.loads(response.text.strip())
            assert sorted(tool["name"] for tool in payload["result"]["tools"]) == sorted(EXPECTED_MCP_TOOLS)
            assert all("outputSchema" in tool for tool in payload["result"]["tools"])


async def test_mcp_requires_auth(mcp_provisioned) -> None:
    app, _, _, _ = mcp_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            result = await _call_tool(client, 2, "knowledge.search", {"query": "hello"})
            assert result["isError"] is True
            assert "invalid_api_key" in result["content"][0]["text"]
            result = await _call_tool(client, 3, "knowledge.search", {"query": "hello"}, api_key="bogus")
            assert result["isError"] is True
            assert "invalid_api_key" in result["content"][0]["text"]


async def test_mcp_search_create_fetch_update_parity(mcp_provisioned) -> None:
    app, _, raw_key, _ = mcp_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            create_key = f"mcp-parity-{uuid4()}"
            created = await _call_tool(
                client,
                2,
                "knowledge.create",
                {
                    "space_id": "space-a",
                    "title": "MCP parity note",
                    "content": "The MCP adapter writes through the same use case as REST.",
                    "tags": ["mcp"],
                    "idempotency_key": create_key,
                },
                api_key=raw_key,
            )
            assert created["isError"] is False
            item = created["structuredContent"]
            assert item["space_id"] == "space-a"
            assert item["version"] == 1

            searched = await _call_tool(
                client, 3, "knowledge.search", {"query": "parity note", "space_ids": ["space-a"]}, api_key=raw_key
            )
            assert searched["isError"] is False
            hits = searched["structuredContent"]["hits"]
            matched = next(hit for hit in hits if hit["canonical_id"] == item["id"])
            assert matched["title"] == "MCP parity note"

            fetched = await _call_tool(
                client,
                4,
                "knowledge.fetch",
                {"item_id": item["id"], "revision_id": matched["revision_id"]},
                api_key=raw_key,
            )
            assert fetched["isError"] is False
            assert fetched["structuredContent"]["canonical_id"] == item["id"]
            assert fetched["structuredContent"]["content"] == "The MCP adapter writes through the same use case as REST."

            update_key = f"mcp-parity-update-{uuid4()}"
            updated = await _call_tool(
                client,
                5,
                "knowledge.update",
                {
                    "item_id": item["id"],
                    "expected_version": 1,
                    "title": "MCP parity note v2",
                    "content": "Updated through the MCP adapter.",
                    "tags": ["mcp"],
                    "idempotency_key": update_key,
                },
                api_key=raw_key,
            )
            assert updated["isError"] is False
            assert updated["structuredContent"]["version"] == 2
            assert updated["structuredContent"]["content"] == "Updated through the MCP adapter."
            update_replay = await _call_tool(
                client,
                15,
                "knowledge.update",
                {
                    "item_id": item["id"],
                    "expected_version": 1,
                    "title": "MCP parity note v2",
                    "content": "Updated through the MCP adapter.",
                    "tags": ["mcp"],
                    "idempotency_key": update_key,
                },
                api_key=raw_key,
            )
            assert update_replay["isError"] is False
            assert update_replay["structuredContent"]["version"] == 2
            assert update_replay["structuredContent"]["replayed"] is True

            replayed = await _call_tool(
                client,
                6,
                "knowledge.create",
                {
                    "space_id": "space-a",
                    "title": "MCP parity note",
                    "content": "The MCP adapter writes through the same use case as REST.",
                    "tags": ["mcp"],
                    "idempotency_key": create_key,
                },
                api_key=raw_key,
            )
            assert replayed["isError"] is False
            assert replayed["structuredContent"]["id"] == item["id"]
            assert replayed["structuredContent"]["replayed"] is True

            contexted = await _call_tool(
                client, 10, "context.assemble", {"query": "parity note", "space_ids": ["space-a"]}, api_key=raw_key
            )
            assert contexted["isError"] is False
            assert contexted["structuredContent"]["total_chars"] <= contexted["structuredContent"]["budget_chars"]

            resolved = await _call_tool(
                client,
                11,
                "evidence.resolve",
                {"citation_uri": fetched["structuredContent"]["citation_uri"]},
                api_key=raw_key,
            )
            assert resolved["isError"] is False
            assert resolved["structuredContent"]["canonical_id"] == item["id"]

            status = await _call_tool(client, 12, "ingestion.status", {"space_id": "space-a"}, api_key=raw_key)
            assert status["isError"] is False

            stale = await _call_tool(
                client,
                13,
                "knowledge.update",
                {
                    "item_id": item["id"],
                    "expected_version": 1,
                    "title": "stale",
                    "content": "stale content",
                    "idempotency_key": f"mcp-parity-stale-{uuid4()}",
                },
                api_key=raw_key,
            )
            assert stale["isError"] is True
            assert "version_mismatch" in stale["content"][0]["text"]

            archive_key = f"mcp-parity-del-{uuid4()}"
            archived = await _call_tool(
                client,
                14,
                "knowledge.archive",
                {"item_id": item["id"], "expected_version": 2, "idempotency_key": archive_key},
                api_key=raw_key,
            )
            assert archived["isError"] is False
            archive_replay = await _call_tool(
                client,
                16,
                "knowledge.archive",
                {"item_id": item["id"], "expected_version": 2, "idempotency_key": archive_key},
                api_key=raw_key,
            )
            assert archive_replay["isError"] is False
            assert archive_replay["structuredContent"]["replayed"] is True


async def test_mcp_rest_and_adapter_share_grant_boundary(mcp_provisioned_narrow) -> None:
    app, _, raw_key, _ = mcp_provisioned_narrow
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            denied = await _call_tool(
                client,
                2,
                "knowledge.create",
                {
                    "space_id": "space-b",
                    "title": "nope",
                    "content": "outside the grant",
                    "idempotency_key": f"mcp-grant-{uuid4()}",
                },
                api_key=raw_key,
            )
            assert denied["isError"] is True
            assert "resource_unavailable" in denied["content"][0]["text"]

            rest_denied = await client.post(
                "/api/v1/knowledge",
                json={"space_id": "space-b", "title": "nope", "content": "outside the grant"},
                headers={"Authorization": f"Bearer {raw_key}", "Idempotency-Key": f"rest-grant-{uuid4()}"},
            )
            assert rest_denied.status_code == 404

            allowed = await _call_tool(
                client,
                3,
                "knowledge.create",
                {
                    "space_id": "space-a",
                    "title": "inside grant",
                    "content": "inside the grant",
                    "idempotency_key": f"mcp-grant-ok-{uuid4()}",
                },
                api_key=raw_key,
            )
            assert allowed["isError"] is False


async def test_mcp_revoked_key_rejected_everywhere(mcp_provisioned) -> None:
    from sqlalchemy import select

    from src.gateway.infrastructure.persistence.identity_models import PersonalAPIKeyModel

    app, factory, raw_key, member_a = mcp_provisioned
    codec = APIKeyCodec({1: SecretValue(PEPPER)}, active_pepper_version=1)
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            before = await _call_tool(client, 2, "knowledge.search", {"query": "x"}, api_key=raw_key)
            assert before["isError"] is False
            async with factory.begin() as session:
                key_id = await session.scalar(
                    select(PersonalAPIKeyModel.id).where(PersonalAPIKeyModel.member_id == member_a)
                )
                assert key_id is not None
                await APIKeyService(session, codec).revoke(_member_principal(member_a), key_id, request_id="mcp-revoke")
            after = await _call_tool(client, 3, "knowledge.search", {"query": "x"}, api_key=raw_key)
            assert after["isError"] is True
            assert "invalid_api_key" in after["content"][0]["text"]
            rest = await client.post("/api/v1/retrieval/search", json={"query": "x"}, headers={"Authorization": f"Bearer {raw_key}"})
            assert rest.status_code == 401


async def test_mcp_schema_rejects_bad_arguments(mcp_provisioned) -> None:
    app, _, raw_key, _ = mcp_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            response = await client.post(
                "/mcp/",
                json=_rpc(2, "tools/call", {"name": "knowledge.search", "arguments": {"limit": 5}}),
                headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {raw_key}"},
            )
            payload = _wire_result(response)
            assert payload["result"]["isError"] is True
            assert "query" in payload["result"]["content"][0]["text"]


async def test_mcp_unknown_tool_is_protocol_error(mcp_provisioned) -> None:
    app, _, raw_key, _ = mcp_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            response = await client.post(
                "/mcp/",
                json=_rpc(2, "tools/call", {"name": "does.not.exist", "arguments": {}}),
                headers={"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {raw_key}"},
            )
            payload = _wire_result(response)
            assert payload["result"]["isError"] is True
            assert "Unknown tool" in payload["result"]["content"][0]["text"]


async def test_mcp_sequential_searches_succeed(mcp_provisioned) -> None:
    app, _, raw_key, _ = mcp_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=30) as client:
            await _initialize(client)
            first = await _call_tool(client, 2, "knowledge.search", {"query": "cancel probe"}, api_key=raw_key)
            assert first["isError"] is False
            second = await _call_tool(client, 3, "knowledge.search", {"query": "cancel probe"}, api_key=raw_key)
            assert second["isError"] is False


async def test_mcp_ingestion_control_cancel_and_retry(mcp_provisioned) -> None:
    from src.gateway.infrastructure.persistence.ingestion_models import (
        DocumentModel,
        DocumentRevisionModel,
        IngestionJobModel,
    )

    app, factory, raw_key, member_a = mcp_provisioned
    document_id = uuid4()
    revision_id = uuid4()
    job_id = uuid4()
    async with factory.begin() as session:
        session.add(DocumentModel(id=document_id, space_id="space-a", display_name="mcp-job.txt", created_by_member_id=member_a))
        await session.flush()
        session.add(
            DocumentRevisionModel(
                id=revision_id,
                document_id=document_id,
                space_id="space-a",
                version=1,
                original_filename="mcp-job.txt",
                mime_type="text/plain",
                size_bytes=4,
                checksum_sha256="b" * 64,
                staging_storage_key=f"staging/{revision_id}",
                created_by_member_id=member_a,
            )
        )
        await session.flush()
        session.add(
            IngestionJobModel(
                id=job_id,
                space_id="space-a",
                document_id=document_id,
                document_revision_id=revision_id,
                initiated_by_member_id=member_a,
                idempotency_key=f"mcp-job-{uuid4()}",
            )
        )
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            listed = await _call_tool(client, 2, "ingestion.status", {"space_id": "space-a"}, api_key=raw_key)
            assert listed["isError"] is False
            assert any(row["id"] == str(job_id) for row in listed["structuredContent"]["items"])
            single = await _call_tool(client, 3, "ingestion.status", {"job_id": str(job_id)}, api_key=raw_key)
            assert single["isError"] is False
            assert single["structuredContent"]["job"]["id"] == str(job_id)
            cancelled = await _call_tool(
                client,
                4,
                "ingestion.control",
                {"job_id": str(job_id), "operation": "cancel", "idempotency_key": f"mcp-cancel-{uuid4()}"},
                api_key=raw_key,
            )
            assert cancelled["isError"] is False
            assert cancelled["structuredContent"]["job"]["state"] == "cancellation_requested"
            invalid_retry = await _call_tool(
                client,
                5,
                "ingestion.control",
                {"job_id": str(job_id), "operation": "retry", "idempotency_key": f"mcp-retry-{uuid4()}"},
                api_key=raw_key,
            )
            assert invalid_retry["isError"] is True
            assert "version_mismatch" in invalid_retry["content"][0]["text"]


async def test_mcp_sdk_client_lists_and_calls_tools(mcp_provisioned) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    app, _, raw_key, _ = mcp_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        http = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        async with http:
            async with streamable_http_client(
                "http://testserver/mcp/", http_client=http, terminate_on_close=False
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == set(EXPECTED_MCP_TOOLS)
                    result = await session.call_tool(
                        "knowledge.search", {"query": "sdk client probe", "space_ids": ["space-a"]}
                    )
                    assert result.is_error is False


async def test_mcp_scope_denial_blocks_write(mcp_provisioned) -> None:
    app, factory, _, member_a = mcp_provisioned
    now = datetime.now(timezone.utc)
    codec = APIKeyCodec({1: SecretValue(PEPPER)}, active_pepper_version=1)
    async with factory.begin() as session:
        website_session = await SessionService(session).issue(_member_principal(member_a), now=now, step_up_at=now)
        created = await APIKeyService(session, codec).create(
            member_a,
            family_id=website_session.family_id,
            name="MCP read-only",
            scopes={"knowledge:read"},
            request_id="mcp-readonly-key",
            now=now,
        )
        read_key = created.secret.reveal()
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            denied = await _call_tool(
                client,
                2,
                "knowledge.create",
                {
                    "space_id": "space-a",
                    "title": "nope",
                    "content": "read-only key",
                    "idempotency_key": f"mcp-readonly-{uuid4()}",
                },
                api_key=read_key,
            )
            assert denied["isError"] is True
            assert "resource_unavailable" in denied["content"][0]["text"]
            allowed = await _call_tool(client, 3, "knowledge.search", {"query": "x"}, api_key=read_key)
            assert allowed["isError"] is False


async def test_mcp_x_api_key_header_authenticates(mcp_provisioned) -> None:
    app, _, raw_key, _ = mcp_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            response = await client.post(
                "/mcp/",
                json=_rpc(2, "tools/call", {"name": "knowledge.search", "arguments": {"query": "x"}}),
                headers={"Accept": "application/json, text/event-stream", "X-API-Key": raw_key},
            )
            assert _wire_result(response)["result"]["isError"] is False


async def test_mcp_quota_exceeded_maps_to_tool_error(mcp_provisioned) -> None:
    from src.gateway.application.services.quota_service import QuotaPolicy, QuotaService
    from src.gateway.mcp import auth as mcp_auth_module

    app, _, raw_key, _ = mcp_provisioned
    strict = QuotaService(
        QuotaPolicy(requests_per_minute=1, concurrent_requests=1, tokens_per_minute=1, storage_bytes=1, burst_requests=0)
    )
    real_factory = mcp_auth_module.quota_service_from_settings
    mcp_auth_module.quota_service_from_settings = lambda gateway_settings: strict
    try:
        async with app.state.mcp_session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                await _initialize(client)
                first = await _call_tool(client, 2, "knowledge.search", {"query": "x"}, api_key=raw_key)
                assert first["isError"] is False
                second = await _call_tool(client, 3, "knowledge.search", {"query": "x"}, api_key=raw_key)
                assert second["isError"] is True
                assert "quota_exceeded" in second["content"][0]["text"]
    finally:
        mcp_auth_module.quota_service_from_settings = real_factory


async def test_mcp_quota_not_bypassed_by_rotating_space(mcp_provisioned) -> None:
    from src.gateway.application.services.quota_service import QuotaPolicy, QuotaService
    from src.gateway.mcp import auth as mcp_auth_module

    app, _, raw_key, _ = mcp_provisioned
    strict = QuotaService(
        QuotaPolicy(requests_per_minute=1, concurrent_requests=4, tokens_per_minute=60000, storage_bytes=1073741824, burst_requests=1)
    )
    real_factory = mcp_auth_module.quota_service_from_settings
    mcp_auth_module.quota_service_from_settings = lambda gateway_settings: strict
    try:
        async with app.state.mcp_session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                await _initialize(client)
                results = []
                for index in range(4):
                    result = await _call_tool(
                        client,
                        10 + index,
                        "knowledge.search",
                        {"query": "x", "active_space_id": f"rotated-{index}"},
                        api_key=raw_key,
                    )
                    results.append(result["isError"])
                assert any(results) is True
    finally:
        mcp_auth_module.quota_service_from_settings = real_factory


async def test_mcp_internal_errors_are_sanitized(mcp_provisioned) -> None:
    from src.gateway.mcp import server as mcp_server_module

    app, _, raw_key, _ = mcp_provisioned
    real_search = mcp_server_module.MCPToolRuntime.knowledge_search

    async def _boom(self, args, headers):
        raise RuntimeError("connection string postgresql://secret:hunter2 exploded")

    mcp_server_module.MCPToolRuntime.knowledge_search = _boom
    try:
        async with app.state.mcp_session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                await _initialize(client)
                result = await _call_tool(client, 2, "knowledge.search", {"query": "x"}, api_key=raw_key)
                assert result["isError"] is True
                assert "internal_error" in result["content"][0]["text"]
                assert "hunter2" not in result["content"][0]["text"]
                assert "postgresql" not in result["content"][0]["text"]
    finally:
        mcp_server_module.MCPToolRuntime.knowledge_search = real_search


async def _sdk_tool_call(session, name: str, arguments: dict):
    result = await session.call_tool(name, arguments)
    assert result.is_error is False
    return result.structured_content


async def test_mcp_dual_harness_conformance_with_grant_boundary_and_revoke(mcp_provisioned) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from sqlalchemy import select

    from src.gateway.infrastructure.persistence.identity_models import PersonalAPIKeyModel
    from src.gateway.mcp.contracts import MCP_PINNED_SPEC_VERSION, MCP_TESTED_CLIENTS

    app, factory, full_key, member_a = mcp_provisioned
    now = datetime.now(timezone.utc)
    codec = APIKeyCodec({1: SecretValue(PEPPER)}, active_pepper_version=1)
    async with factory.begin() as session:
        website_session = await SessionService(session).issue(_member_principal(member_a), now=now, step_up_at=now)
        created = await APIKeyService(session, codec).create(
            member_a,
            family_id=website_session.family_id,
            name="MCP narrow harness",
            scopes={"knowledge:read", "knowledge:write"},
            request_id="mcp-dual-harness-key",
            now=now,
            space_grants={"space-a"},
        )
        narrow_key = created.secret.reveal()
    assert MCP_PINNED_SPEC_VERSION == "2026-07-28"
    assert [entry.client for entry in MCP_TESTED_CLIENTS] == ["python-sdk-mcp==2.2.0", "raw-http-streamable"]
    assert all(entry.protocol_version == MCP_PINNED_SPEC_VERSION for entry in MCP_TESTED_CLIENTS)
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        sdk_http = httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {full_key}"},
        )
        raw_http = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        async with sdk_http, raw_http:
            await _initialize(raw_http)
            async with streamable_http_client(
                "http://testserver/mcp/", http_client=sdk_http, terminate_on_close=False
            ) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as sdk:
                    await sdk.initialize()
                    shared_a = await _sdk_tool_call(
                        sdk,
                        "knowledge.create",
                        {
                            "space_id": "space-a",
                            "title": "Dual harness note",
                            "content": "Written by the SDK harness.",
                            "tags": ["dual"],
                            "idempotency_key": f"dual-a-{uuid4()}",
                        },
                    )
                    shared_b = await _sdk_tool_call(
                        sdk,
                        "knowledge.create",
                        {
                            "space_id": "space-b",
                            "title": "Dual harness private note",
                            "content": "Outside the narrow grant.",
                            "tags": ["dual"],
                            "idempotency_key": f"dual-b-{uuid4()}",
                        },
                    )
                    found = await _call_tool(
                        raw_http, 10, "knowledge.search", {"query": "dual harness", "space_ids": ["space-a"]}, api_key=narrow_key
                    )
                    assert found["isError"] is False
                    assert shared_a["id"] in {hit["canonical_id"] for hit in found["structuredContent"]["hits"]}
                    fetched = await _call_tool(
                        raw_http,
                        11,
                        "knowledge.fetch",
                        {"citation_uri": (await _sdk_tool_call(sdk, "knowledge.search", {"query": "dual harness", "space_ids": ["space-a"]}))["hits"][0]["citation_uri"]},
                        api_key=narrow_key,
                    )
                    assert fetched["isError"] is False
                    assert fetched["structuredContent"]["canonical_id"] == shared_a["id"]
                    written = await _call_tool(
                        raw_http,
                        12,
                        "knowledge.create",
                        {
                            "space_id": "space-a",
                            "title": "Raw harness reply",
                            "content": "Written by the raw HTTP harness.",
                            "tags": ["dual"],
                            "idempotency_key": f"dual-raw-{uuid4()}",
                        },
                        api_key=narrow_key,
                    )
                    assert written["isError"] is False
                    denied_write = await _call_tool(
                        raw_http,
                        13,
                        "knowledge.create",
                        {
                            "space_id": "space-b",
                            "title": "nope",
                            "content": "outside the narrow grant",
                            "idempotency_key": f"dual-denied-{uuid4()}",
                        },
                        api_key=narrow_key,
                    )
                    assert denied_write["isError"] is True
                    assert "resource_unavailable" in denied_write["content"][0]["text"]
                    private_hits = await _sdk_tool_call(
                        sdk, "knowledge.search", {"query": "private note", "space_ids": ["space-b"]}
                    )
                    private_uri = next(
                        hit["citation_uri"]
                        for hit in private_hits["hits"]
                        if hit["canonical_id"] == shared_b["id"]
                    )
                    denied_fetch = await _call_tool(
                        raw_http,
                        14,
                        "knowledge.fetch",
                        {"citation_uri": private_uri},
                        api_key=narrow_key,
                    )
                    assert denied_fetch["isError"] is True
                    assert "resource_unavailable" in denied_fetch["content"][0]["text"]
                    both = await _sdk_tool_call(sdk, "knowledge.search", {"query": "dual harness"})
                    assert {shared_a["id"], shared_b["id"], written["structuredContent"]["id"]} <= {
                        hit["canonical_id"] for hit in both["hits"]
                    }
                    async with factory.begin() as session:
                        key_id = await session.scalar(
                            select(PersonalAPIKeyModel.id).where(
                                PersonalAPIKeyModel.member_id == member_a,
                                PersonalAPIKeyModel.name == "MCP narrow harness",
                            )
                        )
                        assert key_id is not None
                        await APIKeyService(session, codec).revoke(
                            _member_principal(member_a), key_id, request_id="mcp-dual-revoke"
                        )
                    revoked = await _call_tool(
                        raw_http, 15, "knowledge.search", {"query": "dual harness"}, api_key=narrow_key
                    )
                    assert revoked["isError"] is True
                    assert "invalid_api_key" in revoked["content"][0]["text"]
                    still_ok = await _sdk_tool_call(sdk, "knowledge.search", {"query": "dual harness"})
                    assert still_ok["hits"]


OIDC_ISSUER = "https://mcp-idp.example.test"
OIDC_CLIENT_ID = "mcp-remote-harness"
OIDC_JWK_KID = "mcp-oidc-key-1"


def _oidc_jwks(public_key):
    numbers = public_key.public_numbers()
    raw_n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    raw_e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {
        OIDC_JWK_KID: {
            "kty": "RSA",
            "kid": OIDC_JWK_KID,
            "use": "sig",
            "alg": "RS256",
            "n": base64.urlsafe_b64encode(raw_n).rstrip(b"=").decode(),
            "e": base64.urlsafe_b64encode(raw_e).rstrip(b"=").decode(),
        }
    }


def _mint_oidc(private_key, *, groups, audience=OIDC_CLIENT_ID, extra=None):
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "iss": OIDC_ISSUER,
        "aud": audience,
        "sub": "mcp-oidc-sub-1",
        "iat": now,
        "exp": now + 600,
        "email": "mcp.harness@example.test",
        "email_verified": True,
        "groups": groups,
    }
    payload.update(extra or {})
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": OIDC_JWK_KID})


def _oidc_settings() -> Settings:
    return Settings(
        gateway={
            "environment": "test",
            "public_base_url": "https://gateway.test",
            "api_key_peppers": {1: PEPPER},
            "active_api_key_pepper_version": 1,
            "trusted_hosts": ["testserver", "gateway.test"],
            "oidc_enabled": True,
            "oidc_issuer": OIDC_ISSUER,
            "oidc_client_id": OIDC_CLIENT_ID,
            "oidc_client_secret": "test-client-secret-with-length",
            "oidc_redirect_url": "https://gateway.test/api/v1/auth/oidc/callback",
            "oidc_group_claim": "groups",
            "oidc_username_claim": "email",
            "oidc_group_space_map": {"mcp-oidc": [{"space_id": "space-a", "role": "editor"}]},
        }
    )


async def _provision_oidc(monkeypatch, jwks):
    async def _fake_discover(issuer, *, client=None):
        return oidc_service.OidcEndpoints(
            authorization_endpoint=f"{issuer}/authorize",
            token_endpoint=f"{issuer}/token",
            jwks_uri=f"{issuer}/jwks",
            issuer=issuer,
        )

    async def _fake_fetch_jwks(jwks_uri, *, client=None):
        return dict(jwks)

    monkeypatch.setattr(oidc_service, "discover", _fake_discover)
    monkeypatch.setattr(oidc_service, "fetch_jwks", _fake_fetch_jwks)
    oidc_service.clear_jwks_cache()
    mcp_auth_module.clear_oidc_endpoint_cache()
    settings = _oidc_settings()
    async with isolated_postgres_database() as (_, factory):
        member_a = uuid4()
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_a,
                    username="mcp-owner",
                    username_normalized="mcp-owner",
                    display_name="MCP Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="space-a", name="Space A", created_by_member_id=member_a))
            session.add(Workspace(id="space-b", name="Space B", created_by_member_id=member_a))
            session.add(
                EmbeddingGenerationModel(
                    id=uuid4(),
                    purpose="retrieval",
                    model_id="mcp-test-generation",
                    dimensions=EMBED_DIM,
                    status="active",
                )
            )
        app = create_app(settings)
        set_session_factory(factory)
        runtime_token = set_runtime_settings(settings)
        try:
            yield app, factory
        finally:
            set_session_factory(None)
            reset_runtime_settings(runtime_token)


@pytest.fixture()
async def mcp_provisioned_oidc(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = _oidc_jwks(private_key.public_key())
    async for app, factory in _provision_oidc(monkeypatch, jwks):
        yield app, factory, private_key


async def test_mcp_oidc_bearer_accepted_with_audience_check(mcp_provisioned_oidc) -> None:
    app, _, private_key = mcp_provisioned_oidc
    full_access = _mint_oidc(private_key, groups=["mcp-oidc"])
    wrong_audience = _mint_oidc(private_key, groups=["mcp-oidc"], audience="someone-else")
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            created = await _call_tool(
                client,
                2,
                "knowledge.create",
                {
                    "space_id": "space-a",
                    "title": "OIDC remote note",
                    "content": "Written with an OIDC bearer.",
                    "tags": ["oidc"],
                    "idempotency_key": f"mcp-oidc-{uuid4()}",
                },
                api_key=full_access,
            )
            assert created["isError"] is False
            assert created["structuredContent"]["space_id"] == "space-a"
            searched = await _call_tool(
                client, 3, "knowledge.search", {"query": "OIDC remote", "space_ids": ["space-a"]}, api_key=full_access
            )
            assert searched["isError"] is False
            assert created["structuredContent"]["id"] in {
                hit["canonical_id"] for hit in searched["structuredContent"]["hits"]
            }
            rejected = await _call_tool(
                client, 4, "knowledge.search", {"query": "OIDC remote"}, api_key=wrong_audience
            )
            assert rejected["isError"] is True
            assert "invalid_api_key" in rejected["content"][0]["text"]


async def test_mcp_oidc_scope_claim_challenges_writes(mcp_provisioned_oidc) -> None:
    app, _, private_key = mcp_provisioned_oidc
    read_only = _mint_oidc(private_key, groups=["mcp-oidc"], extra={"scope": "knowledge:read"})
    full_scope = _mint_oidc(private_key, groups=["mcp-oidc"])
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            allowed_read = await _call_tool(
                client, 2, "knowledge.search", {"query": "scope probe"}, api_key=read_only
            )
            assert allowed_read["isError"] is False
            challenged = await _call_tool(
                client,
                3,
                "knowledge.create",
                {
                    "space_id": "space-a",
                    "title": "nope",
                    "content": "read-scoped OIDC bearer",
                    "idempotency_key": f"mcp-oidc-scope-{uuid4()}",
                },
                api_key=read_only,
            )
            assert challenged["isError"] is True
            assert "resource_unavailable" in challenged["content"][0]["text"]
            allowed_write = await _call_tool(
                client,
                4,
                "knowledge.create",
                {
                    "space_id": "space-a",
                    "title": "OIDC full note",
                    "content": "unscoped OIDC bearer keeps session scopes",
                    "idempotency_key": f"mcp-oidc-full-{uuid4()}",
                },
                api_key=full_scope,
            )
            assert allowed_write["isError"] is False


async def test_mcp_oidc_protected_resource_advertises_issuer(mcp_provisioned_oidc) -> None:
    app, _, _ = mcp_provisioned_oidc
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            protected = await client.get("/.well-known/oauth-protected-resource/mcp")
            assert protected.status_code == 200
            assert protected.json()["authorization_servers"] == [OIDC_ISSUER]
            assert protected.json()["scopes_supported"] == ["knowledge:read", "knowledge:write"]
            discovery = await client.get("/mcp/discovery")
            assert discovery.status_code == 200
            auth = discovery.json()["auth"]
            assert "oidc_bearer" in auth["schemes"]
            assert auth["flows"]["oidc_bearer"]["enabled"] is True
            assert auth["flows"]["oidc_bearer"]["issuer"] == OIDC_ISSUER
            assert auth["flows"]["oidc_bearer"]["audience"] == OIDC_CLIENT_ID
            assert auth["flows"]["personal_api_key_bridge"]["use"]
            versions = await client.get("/mcp/versions")
            assert versions.status_code == 200
            assert versions.json()["pinned_spec_version"] == "2026-07-28"
            assert [entry["client"] for entry in versions.json()["clients"]] == [
                "python-sdk-mcp==2.2.0",
                "raw-http-streamable",
            ]
