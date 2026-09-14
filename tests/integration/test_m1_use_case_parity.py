from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import (
    AuditEventModel,
    MemberModel,
    PendingAIActionModel,
    SpaceMembershipModel,
)
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, Workspace
from src.gateway.main import create_app
from tests.integration.postgres_test_database import isolated_postgres_database


PEPPER = "test-m1-parity-pepper-with-adequate-length"


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


async def _provision() -> tuple:
    now = datetime.now(timezone.utc)
    codec = APIKeyCodec({1: SecretValue(PEPPER)}, active_pepper_version=1)
    async with isolated_postgres_database() as (_, factory):
        member_a = uuid4()
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_a,
                    username="parity-owner",
                    username_normalized="parity-owner",
                    display_name="Parity Owner",
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
                    model_id="parity-test-generation",
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
            full = await APIKeyService(session, codec).create(
                member_a,
                family_id=website_session.family_id,
                name="Parity full key",
                scopes={"knowledge:read", "knowledge:write"},
                request_id="parity-full-create",
                now=now,
            )
            narrow = await APIKeyService(session, codec).create(
                member_a,
                family_id=website_session.family_id,
                name="Parity narrow key",
                scopes={"knowledge:read", "knowledge:write"},
                space_grants={"space-a"},
                request_id="parity-narrow-create",
                now=now,
            )
            read_only = await APIKeyService(session, codec).create(
                member_a,
                family_id=website_session.family_id,
                name="Parity read key",
                scopes={"knowledge:read"},
                request_id="parity-read-create",
                now=now,
            )
            keys = {
                "full": full.secret.reveal(),
                "narrow": narrow.secret.reveal(),
                "read_only": read_only.secret.reveal(),
            }
        app = create_app(_settings())
        set_session_factory(factory)
        try:
            yield app, factory, keys
        finally:
            set_session_factory(None)


@pytest.fixture()
async def parity_provisioned():
    async for value in _provision():
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


async def _initialize(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/mcp/",
        json=_rpc(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-parity-test", "version": "0"},
            },
        ),
        headers={"Accept": "application/json, text/event-stream"},
    )
    payload = _wire_result(response)
    assert payload["result"]["protocolVersion"] == "2025-06-18"


async def _call_tool(
    client: httpx.AsyncClient, mid: int, name: str, arguments: dict, *, api_key: str
) -> dict:
    response = await client.post(
        "/mcp/",
        json=_rpc(mid, "tools/call", {"name": name, "arguments": arguments}),
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {api_key}",
        },
    )
    return _wire_result(response)["result"]


def _rest_headers(api_key: str, idempotency_key: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def _audit_count(factory, *, action: str, resource_id: str | None = None) -> int:
    async with factory() as session:
        query = select(func.count()).select_from(AuditEventModel).where(AuditEventModel.action == action)
        if resource_id is not None:
            query = query.where(AuditEventModel.resource_id == resource_id)
        return int(await session.scalar(query) or 0)


async def test_rest_and_mcp_search_allowed(parity_provisioned) -> None:
    app, _, keys = parity_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            rest = await client.post(
                "/api/v1/retrieval/search",
                json={"query": "parity probe", "space_ids": ["space-a"]},
                headers=_rest_headers(keys["full"]),
            )
            assert rest.status_code == 200
            assert rest.json()["query"] == "parity probe"
            mcp = await _call_tool(
                client, 2, "knowledge.search", {"query": "parity probe", "space_ids": ["space-a"]}, api_key=keys["full"]
            )
            assert mcp["isError"] is False
            assert mcp["structuredContent"]["query"] == "parity probe"


async def test_rest_and_mcp_search_denied_without_grant(parity_provisioned) -> None:
    app, _, keys = parity_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            rest = await client.post(
                "/api/v1/retrieval/search",
                json={"query": "parity probe"},
                headers=_rest_headers(keys["read_only"]),
            )
            assert rest.status_code == 200
            denied_rest = await client.post(
                "/api/v1/knowledge",
                json={"space_id": "space-b", "title": "nope", "content": "outside the grant"},
                headers=_rest_headers(keys["narrow"], f"parity-deny-rest-{uuid4()}"),
            )
            assert denied_rest.status_code == 404
            denied_mcp = await _call_tool(
                client,
                3,
                "knowledge.create",
                {
                    "space_id": "space-b",
                    "title": "nope",
                    "content": "outside the grant",
                    "idempotency_key": f"parity-deny-mcp-{uuid4()}",
                },
                api_key=keys["narrow"],
            )
            assert denied_mcp["isError"] is True
            assert "resource_unavailable" in denied_mcp["content"][0]["text"]


async def test_rest_and_mcp_create_allow_and_audit(parity_provisioned) -> None:
    app, factory, keys = parity_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            rest = await client.post(
                "/api/v1/knowledge",
                json={"space_id": "space-a", "title": "Parity note", "content": "Written over REST."},
                headers=_rest_headers(keys["full"], f"parity-create-rest-{uuid4()}"),
            )
            assert rest.status_code == 201
            rest_id = rest.json()["id"]
            mcp = await _call_tool(
                client,
                2,
                "knowledge.create",
                {
                    "space_id": "space-a",
                    "title": "Parity note",
                    "content": "Written over MCP.",
                    "idempotency_key": f"parity-create-mcp-{uuid4()}",
                },
                api_key=keys["full"],
            )
            assert mcp["isError"] is False
            mcp_id = mcp["structuredContent"]["id"]
            assert mcp_id != rest_id
            assert await _audit_count(factory, action="knowledge.create", resource_id=rest_id) == 1
            assert await _audit_count(factory, action="knowledge.create", resource_id=mcp_id) == 1


async def test_rest_and_mcp_update_occ_conflict(parity_provisioned) -> None:
    app, _, keys = parity_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            created = await client.post(
                "/api/v1/knowledge",
                json={"space_id": "space-a", "title": "OCC note", "content": "v1"},
                headers=_rest_headers(keys["full"], f"parity-occ-create-{uuid4()}"),
            )
            assert created.status_code == 201
            item_id = created.json()["id"]
            conflict_rest = await client.put(
                f"/api/v1/knowledge/{item_id}",
                json={"expected_version": 999, "title": "OCC note", "content": "stale write"},
                headers=_rest_headers(keys["full"], f"parity-occ-rest-{uuid4()}"),
            )
            assert conflict_rest.status_code == 409
            conflict_mcp = await _call_tool(
                client,
                3,
                "knowledge.update",
                {
                    "item_id": item_id,
                    "expected_version": 999,
                    "title": "OCC note",
                    "content": "stale write",
                    "idempotency_key": f"parity-occ-mcp-{uuid4()}",
                },
                api_key=keys["full"],
            )
            assert conflict_mcp["isError"] is True
            assert "version_mismatch" in conflict_mcp["content"][0]["text"]


async def test_rest_and_mcp_idempotent_retry_with_same_key(parity_provisioned) -> None:
    app, _, keys = parity_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            retry_key = f"parity-retry-rest-{uuid4()}"
            payload = {"space_id": "space-a", "title": "Retry note", "content": "Same key twice."}
            first = await client.post("/api/v1/knowledge", json=payload, headers=_rest_headers(keys["full"], retry_key))
            second = await client.post("/api/v1/knowledge", json=payload, headers=_rest_headers(keys["full"], retry_key))
            assert first.status_code == 201
            assert second.status_code == 201
            assert first.json()["id"] == second.json()["id"]
            mcp_key = f"parity-retry-mcp-{uuid4()}"
            arguments = {
                "space_id": "space-a",
                "title": "Retry note",
                "content": "Same key twice.",
                "idempotency_key": mcp_key,
            }
            mcp_first = await _call_tool(client, 2, "knowledge.create", dict(arguments), api_key=keys["full"])
            mcp_second = await _call_tool(client, 3, "knowledge.create", dict(arguments), api_key=keys["full"])
            assert mcp_first["isError"] is False
            assert mcp_second["isError"] is False
            assert mcp_first["structuredContent"]["id"] == mcp_second["structuredContent"]["id"]
            assert mcp_second["structuredContent"]["replayed"] is True


async def test_denied_action_cannot_be_self_approved(parity_provisioned) -> None:
    app, factory, keys = parity_provisioned
    async with app.state.mcp_session_manager.run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _initialize(client)
            denied_rest = await client.post(
                "/api/v1/knowledge",
                json={"space_id": "space-a", "title": "nope", "content": "read-only key"},
                headers=_rest_headers(keys["read_only"], f"parity-self-rest-{uuid4()}"),
            )
            assert denied_rest.status_code in (403, 404)
            denied_mcp = await _call_tool(
                client,
                2,
                "knowledge.create",
                {
                    "space_id": "space-a",
                    "title": "nope",
                    "content": "read-only key",
                    "idempotency_key": f"parity-self-mcp-{uuid4()}",
                },
                api_key=keys["read_only"],
            )
            assert denied_mcp["isError"] is True
            retry_rest = await client.post(
                "/api/v1/knowledge",
                json={"space_id": "space-a", "title": "nope", "content": "read-only key"},
                headers=_rest_headers(keys["read_only"], f"parity-self-rest-{uuid4()}"),
            )
            assert retry_rest.status_code in (403, 404)
            async with factory() as session:
                pending = int(
                    await session.scalar(select(func.count()).select_from(PendingAIActionModel)) or 0
                )
                assert pending == 0
                stray = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(AuditEventModel)
                        .where(AuditEventModel.action == "knowledge.create")
                    )
                    or 0
                )
                assert stray == 0
