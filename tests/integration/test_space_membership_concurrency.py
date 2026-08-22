import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.services.session_service import SessionService
from src.gateway.application.services.space_service import SpaceService
from src.gateway.domain.authorization import Action
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from tests.integration.postgres_test_database import isolated_postgres_database


def principal(member_id, *, system_role=SystemRole.MEMBER) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=system_role,
        scopes=frozenset({"*"}),
    )


async def test_restricted_space_creation_roles_and_effective_scope() -> None:
    owner_id = uuid4()
    editor_id = uuid4()
    super_admin_id = uuid4()
    now = datetime(2026, 8, 20, 11, 0, tzinfo=timezone.utc)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(Workspace(id="global", name="Family Shared", revision=1))
            session.add_all(
                [
                    MemberModel(id=owner_id, username="owner", username_normalized="owner", display_name="Owner", status="active", system_role="member", force_password_change=False),
                    MemberModel(id=editor_id, username="editor", username_normalized="editor", display_name="Editor", status="active", system_role="member", force_password_change=False),
                    MemberModel(id=super_admin_id, username="root", username_normalized="root", display_name="Root", status="active", system_role="super_admin", force_password_change=False),
                ]
            )
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    id=uuid4(),
                    space_id="global",
                    member_id=owner_id,
                    role=SpaceRole.EDITOR.value,
                )
            )
            created = await SpaceService(session).create(
                principal(owner_id),
                name="Family Project",
                request_id="space-create",
                now=now,
            )
            await SpaceService(session).set_membership(
                principal(owner_id),
                created.space_id,
                editor_id,
                role=SpaceRole.EDITOR,
                request_id="space-invite",
                now=now,
            )

        async with factory.begin() as session:
            owner_scope = await AuthorizationService(session).effective_space_ids(owner_id)
            narrowed = await AuthorizationService(session).effective_space_ids(
                owner_id,
                requested={created.space_id, "not-authorized"},
            )
            super_admin_scope = await AuthorizationService(session).effective_space_ids(super_admin_id)
            assert owner_scope == ("global", created.space_id)
            assert narrowed == (created.space_id,)
            assert super_admin_scope == ()
            assert await AuthorizationService(session).authorize_space(
                principal(editor_id),
                created.space_id,
                Action.CONTENT_WRITE,
            ) is SpaceRole.EDITOR
            with pytest.raises(Exception, match="Resource is unavailable"):
                await AuthorizationService(session).authorize_space(
                    principal(super_admin_id, system_role=SystemRole.SUPER_ADMIN),
                    created.space_id,
                    Action.CONTENT_READ,
                )
            event = await session.scalar(
                select(AuditEventModel).where(AuditEventModel.request_id == "space-invite")
            )
            assert event is not None and event.action == "space.membership_changed"


async def test_concurrent_owner_removal_cannot_leave_space_without_owner() -> None:
    first_id = uuid4()
    second_id = uuid4()
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(id=first_id, username="first", username_normalized="first", display_name="First", status=MemberStatus.ACTIVE.value, system_role=SystemRole.MEMBER.value, force_password_change=False),
                    MemberModel(id=second_id, username="second", username_normalized="second", display_name="Second", status=MemberStatus.ACTIVE.value, system_role=SystemRole.MEMBER.value, force_password_change=False),
                ]
            )
            created = await SpaceService(session).create(
                principal(first_id),
                name="Two Owners",
                request_id="two-owner-create",
                now=now,
            )
            session.add(
                SpaceMembershipModel(
                    id=uuid4(),
                    space_id=created.space_id,
                    member_id=second_id,
                    role=SpaceRole.OWNER.value,
                    updated_at=now,
                )
            )

        async def remove(actor_id, target_id):
            try:
                async with factory.begin() as session:
                    await SpaceService(session).set_membership(
                        principal(actor_id),
                        created.space_id,
                        target_id,
                        role=None,
                        request_id=f"remove-{actor_id}",
                        now=now,
                    )
                return "removed"
            except Exception as exc:
                return str(exc)

        outcomes = await asyncio.gather(
            remove(first_id, second_id),
            remove(second_id, first_id),
        )
        assert sum(value == "removed" for value in outcomes) == 1
        assert any("Resource is unavailable" in value or "at least one owner" in value for value in outcomes)

        async with factory.begin() as session:
            owners = await session.scalars(
                select(SpaceMembershipModel).where(
                    SpaceMembershipModel.space_id == created.space_id,
                    SpaceMembershipModel.role == SpaceRole.OWNER.value,
                )
            )
            assert len(owners.all()) == 1


async def test_ownership_transfer_requires_recent_session_and_emergency_reason() -> None:
    owner_id = uuid4()
    target_id = uuid4()
    admin_id = uuid4()
    now = datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(id=owner_id, username="owner", username_normalized="owner", display_name="Owner", status="active", system_role="member", force_password_change=False),
                    MemberModel(id=target_id, username="target", username_normalized="target", display_name="Target", status="active", system_role="member", force_password_change=False),
                    MemberModel(id=admin_id, username="admin", username_normalized="admin", display_name="Admin", status="active", system_role="super_admin", force_password_change=False),
                ]
            )
            created = await SpaceService(session).create(
                principal(owner_id),
                name="Ownership",
                request_id="ownership-create",
                now=now,
            )
            await SpaceService(session).set_membership(
                principal(owner_id),
                created.space_id,
                target_id,
                role=SpaceRole.EDITOR,
                request_id="ownership-target",
                now=now,
            )
            stale = await SessionService(session).issue(
                principal(owner_id),
                now=now,
                step_up_at=now - timedelta(minutes=11),
            )
            fresh = await SessionService(session).issue(
                principal(owner_id),
                now=now,
                step_up_at=now,
            )
            admin = await SessionService(session).issue(
                principal(admin_id, system_role=SystemRole.SUPER_ADMIN),
                now=now,
                step_up_at=now,
            )

        async with factory.begin() as session:
            with pytest.raises(Exception, match="Recent authentication required"):
                await SpaceService(session).transfer_ownership(
                    principal(owner_id),
                    stale.family_id,
                    created.space_id,
                    target_id,
                    expected_revision=2,
                    request_id="stale-transfer",
                    now=now,
                )

        async with factory.begin() as session:
            await SpaceService(session).transfer_ownership(
                principal(owner_id),
                fresh.family_id,
                created.space_id,
                target_id,
                expected_revision=2,
                request_id="owner-transfer",
                now=now,
            )

        async with factory.begin() as session:
            await SpaceService(session).transfer_ownership(
                principal(admin_id, system_role=SystemRole.SUPER_ADMIN),
                admin.family_id,
                created.space_id,
                owner_id,
                expected_revision=3,
                request_id="emergency-transfer",
                emergency_reason="Restore ownership after account recovery",
                now=now,
            )
            admin_membership = await session.scalar(
                select(SpaceMembershipModel).where(
                    SpaceMembershipModel.space_id == created.space_id,
                    SpaceMembershipModel.member_id == admin_id,
                )
            )
            assert admin_membership is None
            owner = await session.scalar(
                select(SpaceMembershipModel).where(
                    SpaceMembershipModel.space_id == created.space_id,
                    SpaceMembershipModel.role == SpaceRole.OWNER.value,
                )
            )
            assert owner.member_id == owner_id
            event = await session.scalar(
                select(AuditEventModel).where(AuditEventModel.request_id == "emergency-transfer")
            )
            assert event.action == "space.emergency_ownership_transferred"
