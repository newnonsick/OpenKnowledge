from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.services.session_service import SessionService
from src.gateway.domain.exceptions import AuthenticationException, AuthorizationException
from src.gateway.domain.identity import (
    MemberStatus,
    PermissionProfile,
    Principal,
    PrincipalKind,
    ServiceCredentialSpec,
    SystemRole,
)
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from tests.integration.postgres_test_database import isolated_postgres_database


def member_principal(member_id) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
    )


async def _seed_owner(factory, member_id, spaces=("space-a", "space-b")):
    now = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    async with factory.begin() as session:
        session.add(
            MemberModel(
                id=member_id,
                username="owner",
                username_normalized="owner",
                display_name="Owner",
                status=MemberStatus.ACTIVE.value,
                system_role=SystemRole.MEMBER.value,
                force_password_change=False,
            )
        )
        for space_id in spaces:
            session.add(Workspace(id=space_id, name=space_id))
        await session.flush()
        for space_id in spaces:
            session.add(
                SpaceMembershipModel(
                    id=uuid4(), space_id=space_id, member_id=member_id, role="owner"
                )
            )
        website_session = await SessionService(session).issue(
            member_principal(member_id), now=now, step_up_at=now
        )
        return website_session
    return None


async def test_service_key_lifecycle_grant_subset_and_revocation() -> None:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    member_id = uuid4()
    codec = APIKeyCodec(SecretValue("test-deployment-pepper"))
    async with isolated_postgres_database() as (_, factory):
        await _seed_owner(factory, member_id)
        async with factory.begin() as session:
            created = await APIKeyService(session, codec).create_service_key(
                member_principal(member_id),
                application_id="nightly-import",
                credential_name="worker-1",
                spec=ServiceCredentialSpec(
                    space_grants=frozenset({"space-a"}),
                    permission_profile=PermissionProfile.PROJECT_CONTRIBUTOR,
                ),
                request_id="svc-create",
                now=now,
            )
            raw_key = created.secret.reveal()
        async with factory.begin() as session:
            event = await session.scalar(
                select(AuditEventModel).where(AuditEventModel.request_id == "svc-create")
            )
            assert event.action == "service_key.created"
            assert event.details["application_id"] == "nightly-import"
            assert event.details["key_name"] == "worker-1"
            assert event.details["grant_owner"] == str(member_id)
            assert event.details["grants"] == ["space-a"]
            assert event.details["permission_profile"] == "project_contributor"
            assert raw_key not in str(event.details)
            resolved = await APIKeyService(session, codec).resolve(raw_key, now=now + timedelta(minutes=1))
            assert resolved.kind is PrincipalKind.SERVICE
            assert resolved.subject_id == str(member_id)
            assert resolved.space_grants == frozenset({"space-a"})
            assert resolved.permission_profile == PermissionProfile.PROJECT_CONTRIBUTOR
        async with factory.begin() as session:
            await APIKeyService(session, codec).revoke(
                member_principal(member_id), created.key_id, request_id="svc-revoke",
                now=now + timedelta(minutes=2),
            )
        async with factory.begin() as session:
            with pytest.raises(AuthenticationException):
                await APIKeyService(session, codec).resolve(raw_key, now=now + timedelta(minutes=3))


async def test_service_key_rejects_outside_grants_expiry_and_self_issue() -> None:
    now = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)
    member_id = uuid4()
    codec = APIKeyCodec(SecretValue("test-deployment-pepper"))
    async with isolated_postgres_database() as (_, factory):
        await _seed_owner(factory, member_id)
        async with factory.begin() as session:
            service = APIKeyService(session, codec)
            with pytest.raises(ValueError, match="Unknown space in grants"):
                await service.create_service_key(
                    member_principal(member_id),
                    application_id="app",
                    credential_name="k",
                    spec=ServiceCredentialSpec(
                        space_grants=frozenset({"space-a", "no-such-space"}),
                        permission_profile=PermissionProfile.READER,
                    ),
                    request_id="svc-bad-grant",
                    now=now,
                )
            with pytest.raises(ValueError, match="human admin"):
                await service.create_service_key(
                    member_principal(member_id),
                    application_id="app",
                    credential_name="k",
                    spec=ServiceCredentialSpec(
                        space_grants=frozenset({"space-a"}),
                        permission_profile=PermissionProfile.HUMAN_ADMIN,
                    ),
                    request_id="svc-admin",
                    now=now,
                )
            created = await service.create_service_key(
                member_principal(member_id),
                application_id="app",
                credential_name="short-lived",
                spec=ServiceCredentialSpec(
                    space_grants=frozenset({"space-a"}),
                    permission_profile=PermissionProfile.READER,
                    expires_at=now + timedelta(minutes=5),
                ),
                request_id="svc-expiring",
                now=now,
            )
            raw_key = created.secret.reveal()
        async with factory.begin() as session:
            with pytest.raises(AuthenticationException):
                await APIKeyService(session, codec).resolve(raw_key, now=now + timedelta(minutes=6))
        async with factory.begin() as session:
            service = APIKeyService(session, codec)
            created = await service.create_service_key(
                member_principal(member_id),
                application_id="app",
                credential_name="parent",
                spec=ServiceCredentialSpec(
                    space_grants=frozenset({"space-a"}),
                    permission_profile=PermissionProfile.PROJECT_CONTRIBUTOR,
                ),
                request_id="svc-parent",
                now=now,
            )
            parent = await service.resolve(created.secret.reveal(), now=now)
            assert parent.kind is PrincipalKind.SERVICE
            with pytest.raises(AuthorizationException):
                await service.create_service_key(
                    parent,
                    application_id="app",
                    credential_name="child",
                    spec=ServiceCredentialSpec(
                        space_grants=frozenset({"space-a"}),
                        permission_profile=PermissionProfile.READER,
                    ),
                    request_id="svc-child",
                    now=now,
                )
