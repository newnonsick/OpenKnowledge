import base64
from datetime import datetime, timezone
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from src.gateway.application.services import oidc_service
from src.gateway.application.services.oidc_service import authenticate_callback, build_principal
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings, reset_runtime_settings, set_runtime_settings
from src.gateway.domain.exceptions import AuthenticationException
from src.gateway.domain.identity import MemberStatus
from src.gateway.infrastructure.persistence.identity_models import (
    AuditEventModel,
    MemberModel,
    SessionCredentialModel,
    SessionFamilyModel,
    SpaceMembershipModel,
)
from src.gateway.infrastructure.persistence.models import Workspace
from tests.integration.postgres_test_database import isolated_postgres_database


ISSUER = "https://idp.example.test"
CLIENT_ID = "gateway-client"
JWK_KID = "pg-key-1"


def _jwks(public_key, kid=JWK_KID):
    numbers = public_key.public_numbers()
    raw_n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    raw_e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {
        kid: {
            "kty": "RSA",
            "kid": kid,
            "use": "sig",
            "alg": "RS256",
            "n": base64.urlsafe_b64encode(raw_n).rstrip(b"=").decode(),
            "e": base64.urlsafe_b64encode(raw_e).rstrip(b"=").decode(),
        }
    }


def _mint(private_key, *, groups, email="pg.user@example.test", lifetime=600, subject="pg-sub-1", extra=None):
    now = int(datetime.now(timezone.utc).timestamp())
    payload = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": subject,
        "iat": now,
        "exp": now + lifetime,
        "email": email,
        "email_verified": True,
        "groups": groups,
    }
    payload.update(extra or {})
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": JWK_KID})


async def test_oidc_callback_provisions_syncs_and_deprovisions_on_postgres() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = _jwks(private_key.public_key())
    settings = Settings(
        gateway={
            "environment": "test",
            "oidc_enabled": True,
            "oidc_issuer": ISSUER,
            "oidc_client_id": CLIENT_ID,
            "oidc_client_secret": "test-client-secret-with-length",
            "oidc_redirect_url": "https://gateway.test/api/v1/auth/oidc/callback",
            "oidc_group_claim": "groups",
            "oidc_username_claim": "email",
            "oidc_role_mapping": {"sso-admins": "super_admin"},
            "oidc_group_space_map": {
                "team-a": [{"space_id": "space-a", "role": "editor"}],
                "team-b": [{"space_id": "space-b", "role": "reader"}],
            },
            "oidc_deprovision_disable": True,
        }
    )
    token = set_runtime_settings(settings)
    oidc_service.clear_jwks_cache()
    try:
        async with isolated_postgres_database() as (_, factory):
            creator_id = uuid4()
            async with factory.begin() as session:
                session.add(
                    MemberModel(
                        id=creator_id,
                        username="bootstrap",
                        username_normalized="bootstrap",
                        display_name="Bootstrap",
                        status=MemberStatus.ACTIVE.value,
                        system_role="super_admin",
                        force_password_change=False,
                    )
                )
                await session.flush()
                session.add(Workspace(id="space-a", name="A", created_by_member_id=creator_id))
                session.add(Workspace(id="space-b", name="B", created_by_member_id=creator_id))

            async with factory.begin() as session:
                _, first = await authenticate_callback(
                    session,
                    id_token=_mint(private_key, groups=["team-a", "team-b", "sso-admins"]),
                    jwks=jwks,
                    request_id="pg-oidc-1",
                )
                assert first.created is True
                assert first.system_role == "super_admin"
                assert set(first.granted_memberships) == {("space-a", "editor"), ("space-b", "reader")}
                member_id = first.member.id

            async with factory() as session:
                member = await session.get(MemberModel, member_id)
                assert member is not None
                assert member.status == MemberStatus.ACTIVE.value
                assert member.username_normalized == "pg.user@example.test"
                rows = list(
                    await session.scalars(
                        select(SpaceMembershipModel).where(SpaceMembershipModel.member_id == member_id)
                    )
                )
                assert {(row.space_id, row.role) for row in rows} == {("space-a", "editor"), ("space-b", "reader")}

            async with factory.begin() as session:
                member = await session.get(MemberModel, member_id)
                assert member is not None
                await SessionService(session).issue(build_principal(member), now=datetime.now(timezone.utc))

            async with factory.begin() as session:
                _, second = await authenticate_callback(
                    session,
                    id_token=_mint(private_key, groups=["team-b"]),
                    jwks=jwks,
                    request_id="pg-oidc-2",
                )
                assert second.created is False
                assert second.system_role == "member"
                assert set(second.removed_memberships) == {("space-a", "editor")}
                assert second.disabled is False

            async with factory.begin() as session:
                _, third = await authenticate_callback(
                    session,
                    id_token=_mint(private_key, groups=[]),
                    jwks=jwks,
                    request_id="pg-oidc-3",
                )
                assert set(third.removed_memberships) == {("space-b", "reader")}
                assert third.disabled is True

            async with factory() as session:
                member = await session.get(MemberModel, member_id)
                assert member is not None
                assert member.status == MemberStatus.DISABLED.value
                assert member.oidc_subject == "pg-sub-1"
                assert member.oidc_issuer == ISSUER
                rows = list(
                    await session.scalars(
                        select(SpaceMembershipModel).where(SpaceMembershipModel.member_id == member_id)
                    )
                )
                assert rows == []
                families = list(
                    await session.scalars(
                        select(SessionFamilyModel).where(SessionFamilyModel.member_id == member_id)
                    )
                )
                assert len(families) == 1
                assert families[0].revoked_at is not None
                assert families[0].revoke_reason == "oidc_deprovisioned"
                remaining_credentials = list(
                    await session.scalars(
                        select(SessionCredentialModel).where(
                            SessionCredentialModel.family_id == families[0].id
                        )
                    )
                )
                assert remaining_credentials == []
                actions = [
                    row.action
                    for row in await session.scalars(
                        select(AuditEventModel).where(AuditEventModel.action.in_(["oidc.login", "member.deprovisioned"]))
                    )
                ]
                assert actions.count("oidc.login") == 3
                assert actions.count("member.deprovisioned") == 1

            async with factory.begin() as session:
                with pytest.raises(AuthenticationException):
                    await authenticate_callback(
                        session,
                        id_token=_mint(private_key, groups=["team-a"]),
                        jwks=jwks,
                        request_id="pg-oidc-4",
                    )

            async with factory.begin() as session:
                _, upgrade_first = await authenticate_callback(
                    session,
                    id_token=_mint(
                        private_key,
                        groups=["team-a"],
                        email="upgrade.user@example.test",
                        subject="pg-sub-upgrade",
                    ),
                    jwks=jwks,
                    request_id="pg-oidc-upgrade-1",
                )
                assert upgrade_first.created is True
                assert upgrade_first.system_role == "member"
                assert upgrade_first.granted_memberships == (("space-a", "editor"),)
                upgrade_member_id = upgrade_first.member.id

            async with factory.begin() as session:
                _, upgrade_second = await authenticate_callback(
                    session,
                    id_token=_mint(
                        private_key,
                        groups=["team-a", "sso-admins"],
                        email="upgrade.user@example.test",
                        subject="pg-sub-upgrade",
                    ),
                    jwks=jwks,
                    request_id="pg-oidc-upgrade-2",
                )
                assert upgrade_second.created is False
                assert upgrade_second.system_role == "super_admin"

            async with factory() as session:
                upgraded = await session.get(MemberModel, upgrade_member_id)
                assert upgraded is not None
                assert upgraded.system_role == "super_admin"
    finally:
        reset_runtime_settings(token)
        oidc_service.clear_jwks_cache()
