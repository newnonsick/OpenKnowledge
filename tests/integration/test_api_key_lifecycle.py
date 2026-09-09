from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.services.session_service import SessionService
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.database import get_db_session, set_session_factory
from src.gateway.infrastructure.persistence.identity_models import APIKeyScopeModel, AuditEventModel, MemberModel, PersonalAPIKeyModel
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.session_cookies import access_cookie_name
from tests.integration.postgres_test_database import isolated_postgres_database


def member_principal(member_id) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
    )


async def test_personal_api_key_one_time_reveal_resolution_and_revocation() -> None:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    member_id = uuid4()
    codec = APIKeyCodec(SecretValue("test-deployment-pepper"))

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="key-owner",
                    username_normalized="key-owner",
                    display_name="Key Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            website_session = await SessionService(session).issue(
                member_principal(member_id),
                now=now,
                step_up_at=now,
            )
            created = await APIKeyService(session, codec).create(
                member_id,
                family_id=website_session.family_id,
                name="Laptop",
                scopes={"chat:write", "knowledge:read"},
                request_id="key-create",
                now=now,
            )
            raw_key = created.secret.reveal()

        async with factory.begin() as session:
            stored = await session.get(PersonalAPIKeyModel, created.key_id)
            scopes = set(
                await session.scalars(
                    select(APIKeyScopeModel.scope).where(APIKeyScopeModel.api_key_id == created.key_id)
                )
            )
            event = await session.scalar(
                select(AuditEventModel).where(AuditEventModel.request_id == "key-create")
            )
            assert stored is not None
            assert raw_key not in stored.key_digest
            assert stored.public_id in raw_key
            assert scopes == {"chat:write", "knowledge:read"}
            assert raw_key not in str(event.details)

            resolved = await APIKeyService(session, codec).resolve(raw_key, now=now + timedelta(minutes=1))
            assert resolved.kind is PrincipalKind.API_KEY
            assert resolved.subject_id == str(member_id)
            assert resolved.scopes == frozenset({"chat:write", "knowledge:read"})
            assert resolved.credential_id == str(created.key_id)

        async with factory.begin() as session:
            await APIKeyService(session, codec).revoke(
                member_principal(member_id),
                created.key_id,
                request_id="key-revoke",
                now=now + timedelta(minutes=2),
            )

        async with factory.begin() as session:
            with pytest.raises(Exception, match="Invalid API key"):
                await APIKeyService(session, codec).resolve(raw_key, now=now + timedelta(minutes=3))


async def test_api_key_creation_requires_recent_step_up_and_allowlisted_scopes() -> None:
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    member_id = uuid4()
    codec = APIKeyCodec(SecretValue("test-deployment-pepper"))

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="stale-step-up",
                    username_normalized="stale-step-up",
                    display_name="Stale Step Up",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            website_session = await SessionService(session).issue(
                member_principal(member_id),
                now=now,
                step_up_at=now - timedelta(minutes=11),
            )
            service = APIKeyService(session, codec)
            with pytest.raises(Exception, match="Recent authentication required"):
                await service.create(
                    member_id,
                    family_id=website_session.family_id,
                    name="Phone",
                    scopes={"knowledge:read"},
                    request_id="stale-create",
                    now=now,
                )
            with pytest.raises(ValueError, match="Unsupported API key scope"):
                await service.create(
                    member_id,
                    family_id=website_session.family_id,
                    name="Phone",
                    scopes={"superuser:anything"},
                    request_id="invalid-scope",
                    now=now,
                )
            await SessionService(session).revoke_family(
                website_session.family_id,
                reason="sign_out",
                now=now,
            )
            with pytest.raises(Exception, match="Recent authentication required"):
                await service.create(
                    member_id,
                    family_id=website_session.family_id,
                    name="Phone",
                    scopes={"knowledge:read"},
                    request_id="revoked-session-create",
                    now=now,
                )


async def test_personal_key_and_website_cookie_bind_database_principal() -> None:
    now = datetime.now(timezone.utc)
    member_id = uuid4()
    pepper = "runtime-personal-key-pepper-value"
    codec = APIKeyCodec(SecretValue(pepper))

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="middleware-owner",
                    username_normalized="middleware-owner",
                    display_name="Middleware Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            website_session = await SessionService(session).issue(
                member_principal(member_id),
                now=now,
                step_up_at=now,
            )
            created = await APIKeyService(session, codec).create(
                member_id,
                family_id=website_session.family_id,
                name="Middleware key",
                scopes={"knowledge:read"},
                request_id="middleware-key",
                now=now,
            )

        app = FastAPI()
        app.add_middleware(
            APIKeyAuthMiddleware,
            allowed_keys=[],
            api_key_peppers={1: pepper},
            active_api_key_pepper_version=1,
            session_factory=factory,
        )

        @app.get("/principal")
        async def principal_endpoint(
            request: Request,
            session: AsyncSession = Depends(get_db_session),
        ):
            return {
                "subject_id": request.state.principal.subject_id,
                "kind": request.state.principal.kind.value,
                "database_principal": await session.scalar(
                    text("SELECT current_setting('app.principal_id', true)")
                ),
                "raw_key_attached": hasattr(request.state, "api_key"),
            }

        @app.post("/principal")
        async def unsafe_principal_endpoint(request: Request):
            return {"subject_id": request.state.principal.subject_id}

        set_session_factory(factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://localhost:3000",
            ) as client:
                api_response = await client.get(
                    "/principal",
                    headers={
                        "Authorization": f"Bearer {created.secret.reveal()}"
                    },
                )
                session_response = await client.get(
                    "/principal",
                    cookies={
                        access_cookie_name(): website_session.access_token.reveal()
                    },
                )
                rejected_unsafe_response = await client.post(
                    "/principal",
                    cookies={
                        access_cookie_name(): website_session.access_token.reveal()
                    },
                    json={},
                )
                accepted_unsafe_response = await client.post(
                    "/principal",
                    cookies={
                        access_cookie_name(): website_session.access_token.reveal()
                    },
                    headers={
                        "Origin": "http://localhost:3000",
                        "X-CSRF-Token": website_session.csrf_token.reveal(),
                    },
                    json={},
                )
        finally:
            set_session_factory(None)

    assert api_response.status_code == 200
    assert api_response.json() == {
        "subject_id": str(member_id),
        "kind": PrincipalKind.API_KEY.value,
        "database_principal": str(member_id),
        "raw_key_attached": False,
    }
    assert session_response.status_code == 200
    assert session_response.json() == {
        "subject_id": str(member_id),
        "kind": PrincipalKind.SESSION.value,
        "database_principal": str(member_id),
        "raw_key_attached": False,
    }
    assert rejected_unsafe_response.status_code == 403
    assert accepted_unsafe_response.status_code == 200
    assert accepted_unsafe_response.json() == {"subject_id": str(member_id)}


async def test_api_key_revoke_hides_foreign_and_missing_keys() -> None:
    from src.gateway.domain.exceptions import AuthorizationException

    now = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)
    owner_id = uuid4()
    stranger_id = uuid4()
    codec = APIKeyCodec(SecretValue("test-deployment-pepper"))

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            for mid, name in ((owner_id, "key-owner"), (stranger_id, "key-stranger")):
                session.add(
                    MemberModel(
                        id=mid,
                        username=name,
                        username_normalized=name,
                        display_name=name,
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    )
                )
            website_session = await SessionService(session).issue(
                member_principal(owner_id),
                now=now,
                step_up_at=now,
            )
            created = await APIKeyService(session, codec).create(
                owner_id,
                family_id=website_session.family_id,
                name="Phone",
                scopes={"knowledge:read"},
                request_id="hiding-create",
                now=now,
            )

        async with factory.begin() as session:
            with pytest.raises(AuthorizationException):
                await APIKeyService(session, codec).revoke(
                    member_principal(stranger_id),
                    created.key_id,
                    request_id="foreign-revoke",
                    now=now,
                )

        async with factory.begin() as session:
            with pytest.raises(AuthorizationException):
                await APIKeyService(session, codec).revoke(
                    member_principal(owner_id),
                    uuid4(),
                    request_id="missing-revoke",
                    now=now,
                )
