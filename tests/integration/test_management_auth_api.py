from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pyotp
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.security.tokens import SecretValue
from src.gateway.application.security.totp import MFASecretService
from src.gateway.application.services.bootstrap_service import BootstrapService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, PasswordCredentialModel, PersonalAPIKeyModel, SessionCredentialModel, SessionFamilyModel
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.routers.management_auth import router
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_login_throttling_is_enforced_in_postgresql() -> None:
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
                responses = [
                    await client.post(
                        "/api/v1/auth/login",
                        json={"username": "missing", "password": "wrong-password"},
                    )
                    for _ in range(5)
                ]

                assert [response.status_code for response in responses[:4]] == [401, 401, 401, 401]
                assert responses[4].status_code == 429
                assert int(responses[4].headers["Retry-After"]) >= 1
                assert responses[4].json()["error"]["code"] == "login_throttled"
        finally:
            set_session_factory(None)


async def test_first_login_mfa_cookie_session_and_refresh_flow() -> None:
    now = datetime.now(timezone.utc)
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
    password_service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)
    mfa_service = MFASecretService(SecretValue(mfa_key))

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            bootstrap = await BootstrapService(session, password_service).create_first_super_admin(
                username="admin",
                display_name="Admin",
                request_id="bootstrap",
                now=now,
            )
            temporary_password = bootstrap.temporary_password.reveal()

        app = FastAPI()
        app.state.settings = settings
        register_exception_handlers(app)
        app.add_middleware(
            APIKeyAuthMiddleware,
            allowed_keys=[],
            session_factory=factory,
        )
        app.add_middleware(SettingsContextMiddleware)
        app.include_router(router)

        @app.get("/private-activity")
        async def private_activity():
            return {"status": "recorded"}

        set_session_factory(factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
            ) as client:
                first_login = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": temporary_password},
                )
                assert first_login.status_code == 200
                assert first_login.json()["requires_password_change"] is True
                assert first_login.headers["Cache-Control"] == "no-store"
                set_cookies = first_login.headers.get_list("set-cookie")
                assert any("__Host-aigw-access=" in value and "HttpOnly" in value and "Secure" in value and "Path=/" in value for value in set_cookies)
                assert any("__Secure-aigw-refresh=" in value and "HttpOnly" in value and "Secure" in value and "Path=/api/v1/auth/refresh" in value for value in set_cookies)
                csrf = client.cookies.get("aigw-csrf")
                assert csrf

                password_change = await client.post(
                    "/api/v1/auth/password",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                    json={
                        "password": "a permanent password long enough",
                        "confirmation": "a permanent password long enough",
                    },
                )
                assert password_change.status_code == 200
                assert password_change.json()["requires_mfa_enrollment"] is True

                csrf = client.cookies.get("aigw-csrf")
                enrollment = await client.post(
                    "/api/v1/auth/mfa/totp/enroll",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                    json={},
                )
                assert enrollment.status_code == 200
                assert enrollment.headers["Cache-Control"] == "no-store"
                factor_id = enrollment.json()["factor_id"]
                secret = enrollment.json()["secret"]

                confirmation = await client.post(
                    "/api/v1/auth/mfa/totp/confirm",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                    json={
                        "factor_id": factor_id,
                        "code": pyotp.TOTP(secret).now(),
                    },
                )
                assert confirmation.status_code == 200
                assert len(confirmation.json()["recovery_codes"]) == 10
                recovery_codes = confirmation.json()["recovery_codes"]
                initial_key = confirmation.json()["initial_api_key"]
                assert initial_key["secret"].startswith("aigw_v1_")
                assert initial_key["name"] == "First device"
                assert initial_key["scopes"] == ["chat:write", "knowledge:read", "knowledge:write", "spaces:read"]
                assert confirmation.headers["Cache-Control"] == "no-store"

                async with factory() as verification_session:
                    stored_keys = list(await verification_session.scalars(select(PersonalAPIKeyModel)))
                    assert len(stored_keys) == 1
                    assert initial_key["secret"] not in stored_keys[0].key_digest

                csrf = client.cookies.get("aigw-csrf")
                stolen_session_enrollment = await client.post(
                    "/api/v1/auth/mfa/totp/enroll",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                    json={},
                )
                assert stolen_session_enrollment.status_code == 401

                missing_totp = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "username": "admin",
                        "password": "a permanent password long enough",
                    },
                )
                assert missing_totp.status_code == 401

                authenticated_login = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "username": "admin",
                        "password": "a permanent password long enough",
                        "totp_code": pyotp.TOTP(secret).now(),
                    },
                )
                assert authenticated_login.status_code == 200
                assert authenticated_login.json()["requires_password_change"] is False
                assert authenticated_login.json()["requires_mfa_enrollment"] is False
                assert "initial_api_key" not in authenticated_login.json()

                async with factory.begin() as activity_session:
                    active_credential = await activity_session.scalar(
                        select(SessionCredentialModel)
                        .where(
                            SessionCredentialModel.credential_type == "access",
                            SessionCredentialModel.revoked_at.is_(None),
                        )
                        .order_by(SessionCredentialModel.issued_at.desc())
                    )
                    active_family = await activity_session.get(SessionFamilyModel, active_credential.family_id)
                    active_family.idle_expires_at = datetime.now(timezone.utc) + timedelta(days=1)
                    activity_family_id = active_family.id

                activity_response = await client.get(
                    "/private-activity",
                    headers={"X-AIGW-Meaningful-Activity": "1"},
                )
                assert activity_response.status_code == 200
                async with factory() as activity_verification:
                    active_family = await activity_verification.get(SessionFamilyModel, activity_family_id)
                    assert active_family.idle_expires_at > datetime.now(timezone.utc) + timedelta(days=6)

                old_refresh = client.cookies.get("__Secure-aigw-refresh")
                csrf = client.cookies.get("aigw-csrf")
                refreshed = await client.post(
                    "/api/v1/auth/refresh",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                    json={},
                )
                assert refreshed.status_code == 200
                assert client.cookies.get("__Secure-aigw-refresh") != old_refresh
                assert client.cookies.get("aigw-csrf") != csrf

                csrf = client.cookies.get("aigw-csrf")
                stepped_up = await client.post(
                    "/api/v1/auth/step-up",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                    json={
                        "password": "a permanent password long enough",
                        "totp_code": pyotp.TOTP(secret).now(),
                    },
                )
                assert stepped_up.status_code == 200
                assert stepped_up.json()["status"] == "reauthenticated"

            recovery_transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=recovery_transport,
                base_url="https://gateway.test",
            ) as recovery_client:
                recovery_login = await recovery_client.post(
                    "/api/v1/auth/login",
                    json={
                        "username": "admin",
                        "password": "a permanent password long enough",
                        "recovery_code": recovery_codes[0],
                    },
                )
                assert recovery_login.status_code == 200

            replay_transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=replay_transport,
                base_url="https://gateway.test",
            ) as replay_client:
                replay = await replay_client.post(
                    "/api/v1/auth/login",
                    json={
                        "username": "admin",
                        "password": "a permanent password long enough",
                        "recovery_code": recovery_codes[0],
                    },
                )
                assert replay.status_code == 401

            async with factory() as verification_session:
                credential = await verification_session.scalar(
                    select(SessionCredentialModel)
                    .where(SessionCredentialModel.credential_type == "access")
                    .order_by(SessionCredentialModel.issued_at.desc())
                )
                family = await verification_session.get(SessionFamilyModel, credential.family_id)
                assert family.last_step_up_at is not None
        finally:
            set_session_factory(None)


async def test_member_receives_exactly_one_personal_api_key_after_first_password_change() -> None:
    now = datetime.now(timezone.utc)
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
    password_service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)
    member_id = uuid4()
    temporary_password = "one-time-family-secret"

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="member",
                    username_normalized="member",
                    display_name="Member",
                    status=MemberStatus.PENDING.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=True,
                )
            )
            session.add(
                PasswordCredentialModel(
                    id=uuid4(),
                    member_id=member_id,
                    password_hash=password_service.hash(temporary_password, username="member"),
                    temporary=True,
                    expires_at=now + timedelta(hours=1),
                )
            )

        app = FastAPI()
        app.state.settings = settings
        register_exception_handlers(app)
        app.add_middleware(APIKeyAuthMiddleware, allowed_keys=[], session_factory=factory)
        app.add_middleware(SettingsContextMiddleware)
        app.include_router(router)
        set_session_factory(factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
                login = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "member", "password": temporary_password},
                )
                assert login.status_code == 200
                csrf = client.cookies.get("aigw-csrf")
                changed = await client.post(
                    "/api/v1/auth/password",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                    json={
                        "password": "a permanent family password",
                        "confirmation": "a permanent family password",
                    },
                )
                assert changed.status_code == 200
                assert changed.json()["requires_mfa_enrollment"] is False
                assert changed.json()["initial_api_key"]["secret"].startswith("aigw_v1_")

                csrf = client.cookies.get("aigw-csrf")
                changed_again = await client.post(
                    "/api/v1/auth/password",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                    json={
                        "password": "a second permanent family secret",
                        "confirmation": "a second permanent family secret",
                    },
                )
                assert changed_again.status_code == 401

                changed_again = await client.post(
                    "/api/v1/auth/password",
                    headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                    json={
                        "current_password": "a permanent family password",
                        "password": "a second permanent family secret",
                        "confirmation": "a second permanent family secret",
                    },
                )
                assert changed_again.status_code == 200
                assert "initial_api_key" not in changed_again.json()

            async with factory() as verification_session:
                stored_keys = list(
                    await verification_session.scalars(
                        select(PersonalAPIKeyModel).where(PersonalAPIKeyModel.member_id == member_id)
                    )
                )
                assert len(stored_keys) == 1
        finally:
            set_session_factory(None)
