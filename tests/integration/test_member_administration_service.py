from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.services.member_administration_service import MemberAdministrationService
from src.gateway.application.services.session_service import SessionService
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MFAFactorModel, MemberModel, PasswordCredentialModel, PersonalAPIKeyModel, SessionFamilyModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from tests.integration.postgres_test_database import isolated_postgres_database


def principal(member_id, role=SystemRole.SUPER_ADMIN):
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=role,
        scopes=frozenset({"*"}),
    )


async def test_super_admin_creates_member_with_one_time_hashed_credential_and_shared_membership() -> None:
    password_service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    admin_id = uuid4()

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=admin_id,
                    username="admin",
                    username_normalized="admin",
                    display_name="Admin",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.SUPER_ADMIN.value,
                    force_password_change=False,
                )
            )
            session.add(
                Workspace(
                    id="global",
                    name="Family Shared",
                    created_by_member_id=admin_id,
                )
            )
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    id=uuid4(),
                    space_id="global",
                    member_id=admin_id,
                    role=SpaceRole.OWNER.value,
                )
            )
            website_session = await SessionService(session).issue(
                principal(admin_id),
                now=now,
                step_up_at=now,
            )
            created = await MemberAdministrationService(session, password_service).create(
                principal(admin_id),
                family_id=website_session.family_id,
                username="  Nök  ",
                display_name="Nok",
                request_id="member-create",
                now=now,
            )
            temporary_password = created.temporary_password.reveal()

        async with factory.begin() as session:
            member = await session.get(MemberModel, created.member_id)
            credential = await session.scalar(
                select(PasswordCredentialModel).where(
                    PasswordCredentialModel.member_id == created.member_id,
                    PasswordCredentialModel.retired_at.is_(None),
                )
            )
            membership = await session.scalar(
                select(SpaceMembershipModel).where(
                    SpaceMembershipModel.member_id == created.member_id,
                    SpaceMembershipModel.space_id == "global",
                )
            )
            audit = await session.scalar(
                select(AuditEventModel).where(AuditEventModel.request_id == "member-create")
            )
            assert member is not None
            assert member.username == "Nök"
            assert member.username_normalized == "nök"
            assert member.status == MemberStatus.PENDING.value
            assert member.force_password_change is True
            assert credential is not None and credential.temporary is True
            assert credential.expires_at == created.expires_at
            assert temporary_password not in credential.password_hash
            assert membership is not None and membership.role == SpaceRole.EDITOR.value
            assert audit is not None and temporary_password not in str(audit.details)


async def test_member_creation_requires_recent_super_admin_website_session_and_unique_username() -> None:
    password_service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    admin_id = uuid4()
    member_id = uuid4()

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(
                        id=admin_id,
                        username="admin",
                        username_normalized="admin",
                        display_name="Admin",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.SUPER_ADMIN.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=member_id,
                        username="member",
                        username_normalized="member",
                        display_name="Member",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    Workspace(id="global", name="Family Shared", created_by_member_id=admin_id),
                ]
            )
            await session.flush()
            admin_session = await SessionService(session).issue(
                principal(admin_id),
                now=now,
                step_up_at=now,
            )
            member_session = await SessionService(session).issue(
                principal(member_id, SystemRole.MEMBER),
                now=now,
                step_up_at=now,
            )
            service = MemberAdministrationService(session, password_service)
            await service.create(
                principal(admin_id),
                family_id=admin_session.family_id,
                username="Existing",
                display_name="Existing",
                request_id="member-first",
                now=now,
            )
            with pytest.raises(ResourceConflictException):
                await service.create(
                    principal(admin_id),
                    family_id=admin_session.family_id,
                    username="existing",
                    display_name="Duplicate",
                    request_id="member-duplicate",
                    now=now,
                )
            with pytest.raises(AuthorizationException):
                await service.create(
                    principal(member_id, SystemRole.MEMBER),
                    family_id=member_session.family_id,
                    username="denied",
                    display_name="Denied",
                    request_id="member-denied",
                    now=now,
                )


async def test_member_lifecycle_prevents_lockout_and_revokes_credentials_with_hashed_reset() -> None:
    password_service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)
    now = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
    admin_id = uuid4()
    member_id = uuid4()

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(
                        id=admin_id,
                        username="admin",
                        username_normalized="admin",
                        display_name="Admin",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.SUPER_ADMIN.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=member_id,
                        username="member",
                        username_normalized="member",
                        display_name="Member",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                ]
            )
            await session.flush()
            session.add(
                PasswordCredentialModel(
                    id=uuid4(),
                    member_id=member_id,
                    password_hash=password_service.hash("Old-password-934!", username="member"),
                    temporary=False,
                )
            )
            member_session = await SessionService(session).issue(
                principal(member_id, SystemRole.MEMBER),
                now=now,
            )
            admin_session = await SessionService(session).issue(
                principal(admin_id),
                now=now,
                step_up_at=now,
            )
            api_key = PersonalAPIKeyModel(
                id=uuid4(),
                member_id=member_id,
                public_id="pk_test_member_lifecycle",
                key_digest="0" * 64,
                pepper_version=1,
                name="Laptop",
                status="active",
            )
            session.add(api_key)
            await session.flush()
            service = MemberAdministrationService(session, password_service)

            with pytest.raises(ResourceConflictException):
                await service.update(
                    principal(admin_id),
                    family_id=admin_session.family_id,
                    member_id=admin_id,
                    display_name="Admin",
                    status=MemberStatus.ACTIVE,
                    system_role=SystemRole.MEMBER,
                    request_id="last-admin-demotion",
                    now=now,
                )

            with pytest.raises(ResourceConflictException):
                await service.update(
                    principal(admin_id),
                    family_id=admin_session.family_id,
                    member_id=member_id,
                    display_name="Member",
                    status=MemberStatus.ACTIVE,
                    system_role=SystemRole.SUPER_ADMIN,
                    request_id="promote-without-mfa",
                    now=now,
                )

            session.add(
                MFAFactorModel(
                    id=uuid4(),
                    member_id=member_id,
                    factor_type="totp",
                    secret_ciphertext=b"encrypted",
                    encryption_key_version=1,
                    confirmed_at=now,
                )
            )
            await session.flush()
            await service.update(
                principal(admin_id),
                family_id=admin_session.family_id,
                member_id=member_id,
                display_name="Member",
                status=MemberStatus.ACTIVE,
                system_role=SystemRole.SUPER_ADMIN,
                request_id="promote-with-mfa",
                now=now,
            )
            await service.update(
                principal(admin_id),
                family_id=admin_session.family_id,
                member_id=member_id,
                display_name="Member",
                status=MemberStatus.ACTIVE,
                system_role=SystemRole.MEMBER,
                request_id="demote-second-admin",
                now=now,
            )

            reset = await service.reset_password(
                principal(admin_id),
                family_id=admin_session.family_id,
                member_id=member_id,
                request_id="member-reset",
                now=now,
            )
            reset_secret = reset.temporary_password.reveal()
            assert reset_secret
            await service.update(
                principal(admin_id),
                family_id=admin_session.family_id,
                member_id=member_id,
                display_name="Member Disabled",
                status=MemberStatus.DISABLED,
                system_role=SystemRole.MEMBER,
                request_id="member-disable",
                now=now,
            )

        async with factory.begin() as session:
            member = await session.get(MemberModel, member_id)
            current_credential = await session.scalar(
                select(PasswordCredentialModel).where(
                    PasswordCredentialModel.member_id == member_id,
                    PasswordCredentialModel.retired_at.is_(None),
                )
            )
            family = await session.get(SessionFamilyModel, member_session.family_id)
            stored_key = await session.get(PersonalAPIKeyModel, api_key.id)
            audits = list(
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.request_id.in_(["member-reset", "member-disable"])
                    )
                )
            )
            assert member is not None
            assert member.display_name == "Member Disabled"
            assert member.status == MemberStatus.DISABLED.value
            assert member.force_password_change is True
            assert current_credential is not None
            assert current_credential.temporary is True
            assert reset_secret not in current_credential.password_hash
            assert password_service.verify(current_credential.password_hash, reset_secret)
            assert family is not None and family.revoked_at == now
            assert stored_key is not None and stored_key.status == "revoked" and stored_key.revoked_at == now
            assert len(audits) == 2
            assert reset_secret not in str([audit.details for audit in audits])
