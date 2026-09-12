from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MemberModel, SessionCredentialModel, SessionFamilyModel
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.routers.management_auth import router
from src.gateway.presentation.session_cookies import CSRF_COOKIE_NAME
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


def _principal(member_id) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
    )


async def test_http_refresh_replay_persists_revocation_and_audit_despite_401() -> None:
    now = datetime.now(timezone.utc)
    member_id = uuid4()
    mfa_key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        gateway={
            "environment": "test",
            "public_base_url": "https://gateway.test",
            "api_key_peppers": {1: "test-api-key-pepper-with-adequate-length"},
            "active_api_key_pepper_version": 1,
            "mfa_encryption_keys": {1: mfa_key},
            "active_mfa_encryption_key_version": 1,
        }
    )

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
            issued = await SessionService(session).issue(_principal(member_id), now=now)

        app = FastAPI()
        app.state.settings = settings
        register_exception_handlers(app)
        app.add_middleware(APIKeyAuthMiddleware, allowed_keys=[], session_factory=factory)
        app.add_middleware(SettingsContextMiddleware)
        app.include_router(router)
        set_session_factory(factory)
        try:
            transport = httpx.ASGITransport(app=app, client=("198.51.100.27", 40000))
            async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
                client.cookies.set(
                    "__Secure-openknowledge-refresh",
                    issued.refresh_token.reveal(),
                    domain="gateway.test",
                    path="/api/v1/auth/refresh",
                )
                client.cookies.set(CSRF_COOKIE_NAME, issued.csrf_token.reveal(), domain="gateway.test", path="/")

                first = await client.post(
                    "/api/v1/auth/refresh",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": issued.csrf_token.reveal()},
                    json={},
                )
                assert first.status_code == 200
                successor_refresh = client.cookies.get("__Secure-openknowledge-refresh")
                successor_csrf = client.cookies.get(CSRF_COOKIE_NAME)
                assert successor_refresh and successor_refresh != issued.refresh_token.reveal()

                async with factory.begin() as backdate:
                    old_credential = await backdate.scalar(
                        select(SessionCredentialModel).where(
                            SessionCredentialModel.family_id == issued.family_id,
                            SessionCredentialModel.credential_type == "refresh",
                            SessionCredentialModel.used_at.is_not(None),
                        )
                    )
                    assert old_credential is not None
                    old_credential.used_at = datetime.now(timezone.utc) - timedelta(seconds=30)

                client.cookies.set(
                    "__Secure-openknowledge-refresh",
                    issued.refresh_token.reveal(),
                    domain="gateway.test",
                    path="/api/v1/auth/refresh",
                )
                client.cookies.set(CSRF_COOKIE_NAME, successor_csrf, domain="gateway.test", path="/")
                replay = await client.post(
                    "/api/v1/auth/refresh",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": successor_csrf},
                    json={},
                )
                assert replay.status_code == 401

                client.cookies.set(
                    "__Secure-openknowledge-refresh",
                    successor_refresh,
                    domain="gateway.test",
                    path="/api/v1/auth/refresh",
                )
                client.cookies.set(CSRF_COOKIE_NAME, successor_csrf, domain="gateway.test", path="/")
                successor_use = await client.post(
                    "/api/v1/auth/refresh",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": successor_csrf},
                    json={},
                )
                assert successor_use.status_code == 401
        finally:
            set_session_factory(None)

        async with factory() as verification:
            family = await verification.get(SessionFamilyModel, issued.family_id)
            assert family is not None and family.revoked_at is not None
            assert family.revoke_reason == "refresh_reuse"
            revoked = (
                await verification.scalars(
                    select(SessionCredentialModel).where(
                        SessionCredentialModel.family_id == issued.family_id,
                        SessionCredentialModel.revoked_at.is_not(None),
                    )
                )
            ).all()
            assert revoked
            security_event = await verification.scalar(
                select(AuditEventModel).where(AuditEventModel.action == "session.refresh_reuse_detected")
            )
            assert security_event is not None


async def test_http_legitimate_refresh_inside_grace_keeps_session_usable() -> None:
    now = datetime.now(timezone.utc)
    member_id = uuid4()
    mfa_key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        gateway={
            "environment": "test",
            "public_base_url": "https://gateway.test",
            "api_key_peppers": {1: "test-api-key-pepper-with-adequate-length"},
            "active_api_key_pepper_version": 1,
            "mfa_encryption_keys": {1: mfa_key},
            "active_mfa_encryption_key_version": 1,
        }
    )

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
            issued = await SessionService(session).issue(_principal(member_id), now=now)

        app = FastAPI()
        app.state.settings = settings
        register_exception_handlers(app)
        app.add_middleware(APIKeyAuthMiddleware, allowed_keys=[], session_factory=factory)
        app.add_middleware(SettingsContextMiddleware)
        app.include_router(router)
        set_session_factory(factory)
        try:
            transport = httpx.ASGITransport(app=app, client=("198.51.100.28", 40000))
            async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
                client.cookies.set(
                    "__Secure-openknowledge-refresh",
                    issued.refresh_token.reveal(),
                    domain="gateway.test",
                    path="/api/v1/auth/refresh",
                )
                client.cookies.set(CSRF_COOKIE_NAME, issued.csrf_token.reveal(), domain="gateway.test", path="/")

                first = await client.post(
                    "/api/v1/auth/refresh",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": issued.csrf_token.reveal()},
                    json={},
                )
                assert first.status_code == 200
                successor_csrf = client.cookies.get(CSRF_COOKIE_NAME)

                client.cookies.set(
                    "__Secure-openknowledge-refresh",
                    issued.refresh_token.reveal(),
                    domain="gateway.test",
                    path="/api/v1/auth/refresh",
                )
                client.cookies.set(CSRF_COOKIE_NAME, successor_csrf, domain="gateway.test", path="/")
                concurrent = await client.post(
                    "/api/v1/auth/refresh",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": successor_csrf},
                    json={},
                )
                assert concurrent.status_code == 200
        finally:
            set_session_factory(None)

        async with factory() as verification:
            family = await verification.get(SessionFamilyModel, issued.family_id)
            assert family is not None and family.revoked_at is None
            assert family.revoke_reason is None
