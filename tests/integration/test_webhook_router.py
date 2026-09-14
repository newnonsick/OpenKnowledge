from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.routers.webhooks import router
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


def principal(member_id, system_role=SystemRole.MEMBER):
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=system_role,
        scopes=frozenset({"*"}),
    )


async def _app(tmp_path, monkeypatch, factory, member_id):
    mfa_key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        gateway={
            "environment": "test",
            "public_base_url": "https://gateway.test",
            "api_key_peppers": {1: "p" * 32},
            "active_api_key_pepper_version": 1,
            "mfa_encryption_keys": {1: mfa_key},
            "active_mfa_encryption_key_version": 1,
            "storage_dir": str(tmp_path / "storage"),
        }
    )
    now = datetime.now(timezone.utc)
    async with factory.begin() as session:
        member_session = await SessionService(session).issue(
            principal(member_id), now=now, step_up_at=now
        )
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
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="https://gateway.test",
        cookies={"__Host-openknowledge-access": member_session.access_token.reveal()},
    )
    headers = {
        "Origin": "https://gateway.test",
        "X-CSRF-Token": member_session.csrf_token.reveal(),
    }
    return client, headers


async def test_webhook_subscription_lifecycle_and_reminders(tmp_path, monkeypatch) -> None:
    member_id = uuid4()
    now = datetime.now(timezone.utc)
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
            session.add(Workspace(id="global", name="Shared", created_by_member_id=member_id))
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    id=uuid4(), space_id="global", member_id=member_id, role=SpaceRole.OWNER.value
                )
            )
        client, headers = await _app(tmp_path, monkeypatch, factory, member_id)
        try:
            denied = await client.post(
                "/api/v1/webhook-subscriptions",
                headers=headers,
                json={"space_id": "global", "url": "http://localhost:9/hook", "secret": "s"},
            )
            assert denied.status_code == 422
            created = await client.post(
                "/api/v1/webhook-subscriptions",
                headers=headers,
                json={
                    "space_id": "global",
                    "url": "https://example.com/hook",
                    "secret": "s3cret",
                    "event_filter": ["ingestion.succeeded"],
                },
            )
            assert created.status_code == 201
            subscription_id = created.json()["id"]
            listed = await client.get(
                "/api/v1/webhook-subscriptions", headers=headers, params={"space_id": "global"}
            )
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()["items"]] == [subscription_id]
            reminders = await client.get("/api/v1/knowledge/review-reminders", headers=headers)
            assert reminders.status_code == 200
            assert reminders.json()["items"] == []
            revoked = await client.delete(
                f"/api/v1/webhook-subscriptions/{subscription_id}", headers=headers
            )
            assert revoked.status_code == 204
            relisted = await client.get(
                "/api/v1/webhook-subscriptions", headers=headers, params={"space_id": "global"}
            )
            assert [item["status"] for item in relisted.json()["items"]] == ["revoked"]
        finally:
            await client.aclose()


async def test_review_reminders_surface_stale_items(tmp_path, monkeypatch) -> None:
    member_id = uuid4()
    from src.gateway.infrastructure.persistence.models import KnowledgeItem as KnowledgeItemModel
    from src.gateway.infrastructure.persistence.models import KnowledgeRevision as KnowledgeRevisionModel

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
            session.add(Workspace(id="global", name="Shared", created_by_member_id=member_id))
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    id=uuid4(), space_id="global", member_id=member_id, role=SpaceRole.OWNER.value
                )
            )
            stale_at = datetime.now(timezone.utc) - timedelta(days=400)
            item_id = uuid4()
            revision_id = uuid4()
            session.add(
                KnowledgeItemModel(
                    id=item_id,
                    workspace_id="global",
                    title="Old valve",
                    content="Turn clockwise.",
                    revision=1,
                    current_revision_id=None,
                    created_at=stale_at,
                    updated_at=stale_at,
                )
            )
            await session.flush()
            session.add(
                KnowledgeRevisionModel(
                    id=revision_id,
                    item_id=item_id,
                    space_id="global",
                    version=1,
                    title="Old valve",
                    content="Turn clockwise.",
                    content_hash="0" * 64,
                    tags=[],
                    author_member_id=member_id,
                    created_at=stale_at,
                )
            )
            await session.flush()
            from sqlalchemy import text as sql_text

            await session.execute(
                sql_text(
                    "UPDATE knowledge_items SET current_revision_id = :revision_id, "
                    "updated_at = :stale_at WHERE id = :item_id"
                ),
                {"revision_id": revision_id, "stale_at": stale_at, "item_id": item_id},
            )
        client, headers = await _app(tmp_path, monkeypatch, factory, member_id)
        try:
            reminders = await client.get("/api/v1/knowledge/review-reminders", headers=headers)
            assert reminders.status_code == 200
            items = reminders.json()["items"]
            assert [item["id"] for item in items] == [str(item_id)]
            assert items[0]["remedy"] == "reviewed"
            scoped = await client.get(
                "/api/v1/knowledge/review-reminders",
                headers=headers,
                params={"older_than_days": 500},
            )
            assert scoped.json()["items"] == []
        finally:
            await client.aclose()
