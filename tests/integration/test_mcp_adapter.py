from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, Workspace
from src.gateway.main import create_app
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
