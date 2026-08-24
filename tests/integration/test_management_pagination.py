from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, PendingAIActionModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.routers.management import router
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


def session_principal(member_id):
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
    )


@asynccontextmanager
async def pagination_client(tmp_path):
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    alpha_id = uuid4()
    beta_id = uuid4()
    settings = Settings(
        gateway={
            "environment": "test",
            "public_base_url": "https://gateway.test",
            "api_key_peppers": {1: "p" * 32},
            "active_api_key_pepper_version": 1,
            "mfa_encryption_keys": {1: Fernet.generate_key().decode("ascii")},
            "active_mfa_encryption_key_version": 1,
            "storage_dir": str(tmp_path / "storage"),
        }
    )

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(
                        id=owner_id,
                        username="owner",
                        username_normalized="owner",
                        display_name="Owner",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=alpha_id,
                        username="alpha",
                        username_normalized="alpha",
                        display_name="Alpha Reader",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=beta_id,
                        username="beta",
                        username_normalized="beta",
                        display_name="Beta Editor",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    Workspace(id="private", name="Private", created_by_member_id=owner_id),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    SpaceMembershipModel(id=uuid4(), space_id="private", member_id=owner_id, role=SpaceRole.OWNER.value),
                    SpaceMembershipModel(id=uuid4(), space_id="private", member_id=alpha_id, role=SpaceRole.READER.value),
                    SpaceMembershipModel(id=uuid4(), space_id="private", member_id=beta_id, role=SpaceRole.EDITOR.value),
                    PendingAIActionModel(
                        id=uuid4(),
                        actor_member_id=owner_id,
                        proposed_by_kind="session",
                        tool_name="knowledge.archive.v1",
                        normalized_command={"target": "one"},
                        command_hash="1" * 64,
                        target_ids=["one"],
                        expected_revision=1,
                        state="pending",
                        expires_at=now + timedelta(hours=1),
                        created_at=now - timedelta(minutes=2),
                    ),
                    PendingAIActionModel(
                        id=uuid4(),
                        actor_member_id=owner_id,
                        proposed_by_kind="session",
                        tool_name="knowledge.archive.v1",
                        normalized_command={"target": "two"},
                        command_hash="2" * 64,
                        target_ids=["two"],
                        expected_revision=1,
                        state="pending",
                        expires_at=now + timedelta(hours=1),
                        created_at=now - timedelta(minutes=1),
                    ),
                    PendingAIActionModel(
                        id=uuid4(),
                        actor_member_id=owner_id,
                        proposed_by_kind="session",
                        tool_name="knowledge.archive.v1",
                        normalized_command={"target": "three"},
                        command_hash="3" * 64,
                        target_ids=["three"],
                        expected_revision=1,
                        state="pending",
                        expires_at=now + timedelta(hours=1),
                        created_at=now,
                    ),
                ]
            )
            issued = await SessionService(session).issue(session_principal(owner_id), now=now, step_up_at=now)

        app = FastAPI()
        app.state.settings = settings
        register_exception_handlers(app)
        app.add_middleware(
            APIKeyAuthMiddleware,
            allowed_keys=[],
            api_key_peppers={1: "p" * 32},
            session_factory=factory,
        )
        app.add_middleware(SettingsContextMiddleware)
        app.include_router(router)
        set_session_factory(factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-aigw-access": issued.access_token.reveal()},
            ) as client:
                yield client
        finally:
            set_session_factory(None)


async def test_space_members_support_cursor_search_and_role_filters(tmp_path) -> None:
    async with pagination_client(tmp_path) as client:
        first = await client.get("/api/v1/spaces/private/members", params={"limit": 2})

        assert first.status_code == 200
        assert [item["username"] for item in first.json()["items"]] == ["alpha", "beta"]
        assert first.json()["next_cursor"]

        second = await client.get(
            "/api/v1/spaces/private/members",
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
        )

        assert second.status_code == 200
        assert [item["username"] for item in second.json()["items"]] == ["owner"]
        assert second.json()["next_cursor"] is None

        searched = await client.get("/api/v1/spaces/private/members", params={"q": "BETA"})
        readers = await client.get("/api/v1/spaces/private/members", params={"role": "reader"})

        assert [item["username"] for item in searched.json()["items"]] == ["beta"]
        assert [item["username"] for item in readers.json()["items"]] == ["alpha"]


async def test_pending_ai_actions_support_stable_cursor_pages(tmp_path) -> None:
    async with pagination_client(tmp_path) as client:
        first = await client.get("/api/v1/ai-actions", params={"limit": 2})

        assert first.status_code == 200
        assert [item["target_ids"] for item in first.json()["items"]] == [["three"], ["two"]]
        assert first.json()["next_cursor"]

        second = await client.get(
            "/api/v1/ai-actions",
            params={"limit": 2, "cursor": first.json()["next_cursor"]},
        )

        assert second.status_code == 200
        assert [item["target_ids"] for item in second.json()["items"]] == [["one"]]
        assert second.json()["next_cursor"] is None
