from uuid import uuid4

import httpx
from fastapi import FastAPI, Request

from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.routers.management import router
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_destructive_ai_tool_requires_bound_one_time_website_confirmation() -> None:
    member_id = uuid4()
    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="member",
                    username_normalized="member",
                    display_name="Member",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="private", name="Private", created_by_member_id=member_id))
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    id=uuid4(),
                    space_id="private",
                    member_id=member_id,
                    role=SpaceRole.OWNER.value,
                )
            )

        app = FastAPI()
        register_exception_handlers(app)

        @app.middleware("http")
        async def test_principal(request: Request, call_next):
            kind = PrincipalKind(request.headers.get("X-Test-Principal-Kind", "api_key"))
            request.state.principal = Principal(
                subject_id=str(member_id),
                kind=kind,
                system_role=SystemRole.MEMBER,
                scopes=frozenset({"*"}),
                credential_id=str(uuid4()),
            )
            return await call_next(request)

        app.include_router(router)
        set_session_factory(factory)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://gateway.test",
            ) as client:
                catalog = await client.get("/api/v1/ai-tools")
                assert catalog.status_code == 200
                assert {item["name"] for item in catalog.json()["items"]} == {
                    "spaces.list.v1",
                    "spaces.create.v1",
                    "spaces.archive.v1",
                }
                listed = await client.post(
                    "/api/v1/ai-tools/spaces.list.v1",
                    headers={"Idempotency-Key": "list-spaces"},
                    json={"arguments": {"limit": 20}},
                )
                assert listed.status_code == 200
                assert [(item["id"], item["role"]) for item in listed.json()["items"]] == [
                    ("private", "owner")
                ]
                created = await client.post(
                    "/api/v1/ai-tools/spaces.create.v1",
                    headers={"Idempotency-Key": "create-space"},
                    json={"arguments": {"name": "Travel"}},
                )
                assert created.status_code == 201
                assert created.json()["result"]["name"] == "Travel"

                proposed = await client.post(
                    "/api/v1/ai-tools/spaces.archive.v1",
                    headers={"Idempotency-Key": "archive-private"},
                    json={"arguments": {"space_id": "private", "expected_revision": 1}},
                )
                assert proposed.status_code == 202
                assert proposed.json()["status"] == "confirmation_required"
                pending_id = proposed.json()["pending_action_id"]

                model_cannot_confirm = await client.post(
                    f"/api/v1/ai-actions/{pending_id}/confirm",
                    headers={
                        "Idempotency-Key": "confirm-private-api-key",
                        "X-Test-Principal-Kind": "api_key",
                    },
                )
                assert model_cannot_confirm.status_code == 404
                assert model_cannot_confirm.json()["error"]["type"] == "authorization_error"

                confirmed = await client.post(
                    f"/api/v1/ai-actions/{pending_id}/confirm",
                    headers={
                        "Idempotency-Key": "confirm-private-session",
                        "X-Test-Principal-Kind": "session",
                    },
                )
                assert confirmed.status_code == 200
                assert confirmed.json() == {
                    "pending_action_id": pending_id,
                    "status": "executed",
                    "tool_name": "spaces.archive.v1",
                }

                replay = await client.post(
                    f"/api/v1/ai-actions/{pending_id}/confirm",
                    headers={
                        "Idempotency-Key": "confirm-private-again",
                        "X-Test-Principal-Kind": "session",
                    },
                )
                assert replay.status_code == 409
        finally:
            set_session_factory(None)

        async with factory.begin() as session:
            space = await session.get(Workspace, "private")
            assert space is not None and space.archived_at is not None and space.revision == 2
