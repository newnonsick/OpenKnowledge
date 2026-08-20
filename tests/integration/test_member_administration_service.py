from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.services.member_administration_service import MemberAdministrationService
from src.gateway.application.services.session_service import SessionService
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MemberModel, PasswordCredentialModel, SpaceMembershipModel
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
