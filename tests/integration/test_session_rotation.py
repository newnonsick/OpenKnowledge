import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.gateway.application.services.session_service import RefreshStatus, SessionService
from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.security.totp import MFASecretService
from src.gateway.application.services.identity_service import IdentityService
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MemberModel, SessionCredentialModel, SessionFamilyModel
from tests.integration.postgres_test_database import isolated_postgres_database


def active_principal(member_id) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
    )


async def test_session_rotation_replay_and_csrf_contract() -> None:
    now = datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)
    member_id = uuid4()

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
            issued = await SessionService(session).issue(active_principal(member_id), now=now)
            assert issued.access_expires_at == now + timedelta(minutes=15)
            assert issued.idle_expires_at == now + timedelta(days=7)
            assert issued.absolute_expires_at == now + timedelta(days=30)
            assert await SessionService(session).verify_csrf(
                issued.family_id,
                issued.csrf_token.reveal(),
            ) is True

        async with factory.begin() as session:
            credentials = (
                await session.execute(
                    select(SessionCredentialModel).where(SessionCredentialModel.family_id == issued.family_id)
                )
            ).scalars().all()
            stored = " ".join(value.token_digest for value in credentials)
            assert issued.access_token.reveal() not in stored
            assert issued.refresh_token.reveal() not in stored

        async with factory.begin() as session:
            first = await SessionService(session).rotate_refresh(
                issued.refresh_token.reveal(),
                now=now + timedelta(minutes=1),
                request_id="refresh-first",
            )
            assert first.status is RefreshStatus.ROTATED
            assert first.session is not None
            successor = first.session

        async with factory.begin() as session:
            concurrent_retry = await SessionService(session).rotate_refresh(
                issued.refresh_token.reveal(),
                now=now + timedelta(minutes=1, seconds=1),
                request_id="refresh-concurrent",
            )
            assert concurrent_retry.status is RefreshStatus.ALREADY_ROTATED
            family = await session.get(SessionFamilyModel, issued.family_id)
            assert family is not None and family.revoked_at is None

        async with factory.begin() as session:
            replay = await SessionService(session).rotate_refresh(
                issued.refresh_token.reveal(),
                now=now + timedelta(minutes=1, seconds=10),
                request_id="refresh-replay",
            )
            assert replay.status is RefreshStatus.REUSE_DETECTED

        async with factory.begin() as session:
            family = await session.get(SessionFamilyModel, issued.family_id)
            assert family is not None and family.revoked_at is not None
            security_event = await session.scalar(
                select(AuditEventModel).where(AuditEventModel.request_id == "refresh-replay")
            )
            assert security_event is not None
            assert security_event.action == "session.refresh_reuse_detected"
            with pytest.raises(Exception, match="Invalid session"):
                await SessionService(session).resolve_access(
                    successor.access_token.reveal(),
                    now=now + timedelta(minutes=2),
                )


async def test_concurrent_refresh_has_one_successor_without_family_revocation() -> None:
    now = datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc)
    member_id = uuid4()

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="race",
                    username_normalized="race",
                    display_name="Race",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            issued = await SessionService(session).issue(active_principal(member_id), now=now)

        async def rotate():
            async with factory.begin() as session:
                return await SessionService(session).rotate_refresh(
                    issued.refresh_token.reveal(),
                    now=now + timedelta(minutes=1),
                    request_id=f"concurrent-{uuid4()}",
                )

        outcomes = await asyncio.gather(rotate(), rotate())
        assert sorted(value.status.value for value in outcomes) == ["already_rotated", "rotated"]
        async with factory.begin() as session:
            family = await session.get(SessionFamilyModel, issued.family_id)
            assert family is not None and family.revoked_at is None


async def test_session_issue_and_activity_fail_closed_for_inactive_state() -> None:
    now = datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc)
    member_id = uuid4()

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="inactive",
                    username_normalized="inactive",
                    display_name="Inactive",
                    status=MemberStatus.DISABLED.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            await session.flush()
            with pytest.raises(Exception, match="Invalid session"):
                await SessionService(session).issue(active_principal(member_id), now=now)

        async with factory.begin() as session:
            member = await session.get(MemberModel, member_id)
            member.status = MemberStatus.ACTIVE.value
            issued = await SessionService(session).issue(active_principal(member_id), now=now)

        async with factory.begin() as session:
            with pytest.raises(Exception, match="Invalid session"):
                await SessionService(session).record_activity(
                    issued.family_id,
                    meaningful=True,
                    now=issued.idle_expires_at + timedelta(seconds=1),
                )


async def test_refresh_and_password_change_do_not_deadlock_or_leave_successor_active() -> None:
    now = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)
    member_id = uuid4()
    password_service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)
    family_locked = asyncio.Event()
    member_locked = asyncio.Event()

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="lock-order",
                    username_normalized="lock-order",
                    display_name="Lock Order",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            issued = await SessionService(session).issue(active_principal(member_id), now=now)

        async def rotate_with_family_lock():
            async with factory.begin() as session:
                await session.scalar(
                    select(SessionFamilyModel)
                    .where(SessionFamilyModel.id == issued.family_id)
                    .with_for_update()
                )
                family_locked.set()
                await member_locked.wait()
                return await SessionService(session).rotate_refresh(
                    issued.refresh_token.reveal(),
                    now=now + timedelta(minutes=1),
                    request_id="lock-order-refresh",
                )

        async def change_with_member_lock():
            await family_locked.wait()
            async with factory.begin() as session:
                await session.scalar(
                    select(MemberModel)
                    .where(MemberModel.id == member_id)
                    .with_for_update()
                )
                member_locked.set()
                await IdentityService(
                    session,
                    password_service,
                    MFASecretService.generate(),
                ).change_password(
                    member_id,
                    new_password="a replacement password long enough",
                    confirmation="a replacement password long enough",
                    request_id="lock-order-password",
                    now=now + timedelta(minutes=1),
                )

        refresh, _ = await asyncio.wait_for(
            asyncio.gather(rotate_with_family_lock(), change_with_member_lock()),
            timeout=10,
        )
        assert refresh.status is RefreshStatus.ROTATED
        assert refresh.session is not None

        async with factory.begin() as session:
            family = await session.get(SessionFamilyModel, issued.family_id)
            assert family is not None
            assert family.revoke_reason == "password_changed"
            with pytest.raises(Exception, match="Invalid session"):
                await SessionService(session).resolve_access(
                    refresh.session.access_token.reveal(),
                    now=now + timedelta(minutes=2),
                )
