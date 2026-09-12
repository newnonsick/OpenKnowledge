from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.services.session_service import SessionService
from src.gateway.domain.authorization import Action
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.identity_models import APIKeySpaceGrantModel, MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from tests.integration.postgres_test_database import isolated_postgres_database


def session_principal(member_id) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
    )


async def test_key_granted_space_a_cannot_use_space_b() -> None:
    now = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)
    owner_id = uuid4()
    codec = APIKeyCodec(SecretValue("test-space-grant-pepper"))

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=owner_id,
                    username="grant-owner",
                    username_normalized="grant-owner",
                    display_name="Grant Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="space-a", name="Space A", created_by_member_id=owner_id))
            session.add(Workspace(id="space-b", name="Space B", created_by_member_id=owner_id))
            await session.flush()
            session.add_all(
                [
                    SpaceMembershipModel(
                        id=uuid4(),
                        space_id="space-a",
                        member_id=owner_id,
                        role="owner",
                    ),
                    SpaceMembershipModel(
                        id=uuid4(),
                        space_id="space-b",
                        member_id=owner_id,
                        role="owner",
                    ),
                ]
            )
            website_session = await SessionService(session).issue(
                session_principal(owner_id),
                now=now,
                step_up_at=now,
            )
            created = await APIKeyService(session, codec).create(
                owner_id,
                family_id=website_session.family_id,
                name="Scoped key",
                scopes={"knowledge:read", "knowledge:write"},
                space_grants={"space-a"},
                request_id="grant-create",
                now=now,
            )
            raw_key = created.secret.reveal()
            assert created.space_grants == frozenset({"space-a"})

        async with factory.begin() as session:
            resolved = await APIKeyService(session, codec).resolve(raw_key, now=now)
            assert resolved.space_grants == frozenset({"space-a"})
            allowed = await AuthorizationService(session).authorize_space(
                resolved, "space-a", Action.CONTENT_READ
            )
            assert allowed.value == "owner"
            with pytest.raises(AuthorizationException):
                await AuthorizationService(session).authorize_space(
                    resolved, "space-b", Action.CONTENT_READ
                )
            scoped = await AuthorizationService(session).effective_space_ids(
                owner_id, principal=resolved
            )
            assert scoped == ("space-a",)

        async with factory.begin() as session:
            await session.execute(
                text("DELETE FROM space_memberships WHERE space_id = 'space-a' AND member_id = :member"),
                {"member": owner_id},
            )

        async with factory.begin() as session:
            narrowed = await APIKeyService(session, codec).resolve(raw_key, now=now)
            assert narrowed.space_grants == frozenset()
            with pytest.raises(AuthorizationException):
                await AuthorizationService(session).authorize_space(
                    narrowed, "space-a", Action.CONTENT_READ
                )


async def test_unrestricted_key_keeps_all_member_spaces() -> None:
    now = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
    owner_id = uuid4()
    codec = APIKeyCodec(SecretValue("test-space-grant-pepper"))

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=owner_id,
                    username="open-owner",
                    username_normalized="open-owner",
                    display_name="Open Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="space-a", name="Space A", created_by_member_id=owner_id))
            session.add(Workspace(id="space-b", name="Space B", created_by_member_id=owner_id))
            await session.flush()
            session.add_all(
                [
                    SpaceMembershipModel(
                        id=uuid4(), space_id="space-a", member_id=owner_id, role="owner"
                    ),
                    SpaceMembershipModel(
                        id=uuid4(), space_id="space-b", member_id=owner_id, role="owner"
                    ),
                ]
            )
            website_session = await SessionService(session).issue(
                session_principal(owner_id),
                now=now,
                step_up_at=now,
            )
            created = await APIKeyService(session, codec).create(
                owner_id,
                family_id=website_session.family_id,
                name="Open key",
                scopes={"knowledge:read"},
                request_id="open-create",
                now=now,
            )
            raw_key = created.secret.reveal()
            assert created.space_grants is None

        async with factory.begin() as session:
            resolved = await APIKeyService(session, codec).resolve(raw_key, now=now)
            assert resolved.space_grants is None
            scoped = await AuthorizationService(session).effective_space_ids(
                owner_id, principal=resolved
            )
            assert set(scoped) == {"space-a", "space-b"}


async def test_space_grant_requires_member_access_and_known_space() -> None:
    now = datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc)
    owner_id = uuid4()
    codec = APIKeyCodec(SecretValue("test-space-grant-pepper"))

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=owner_id,
                    username="strict-owner",
                    username_normalized="strict-owner",
                    display_name="Strict Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="space-a", name="Space A", created_by_member_id=owner_id))
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    id=uuid4(), space_id="space-a", member_id=owner_id, role="owner"
                )
            )
            website_session = await SessionService(session).issue(
                session_principal(owner_id),
                now=now,
                step_up_at=now,
            )
            service = APIKeyService(session, codec)
            with pytest.raises(ValueError, match="Unknown space"):
                await service.create(
                    owner_id,
                    family_id=website_session.family_id,
                    name="Bad space",
                    scopes={"knowledge:read"},
                    space_grants={"missing-space"},
                    request_id="bad-grant",
                    now=now,
                )
            with pytest.raises(ValueError, match="must not be empty"):
                await service.create(
                    owner_id,
                    family_id=website_session.family_id,
                    name="Empty grants",
                    scopes={"knowledge:read"},
                    space_grants=set(),
                    request_id="empty-grant",
                    now=now,
                )
            grants = await service.key_space_grants(uuid4())
            assert grants is None


async def test_rls_denies_key_outside_its_grants() -> None:
    now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    owner_id = uuid4()
    codec = APIKeyCodec(SecretValue("test-space-grant-pepper"))

    async with isolated_postgres_database() as (engine, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=owner_id,
                    username="rls-owner",
                    username_normalized="rls-owner",
                    display_name="RLS Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="space-a", name="Space A", created_by_member_id=owner_id))
            session.add(Workspace(id="space-b", name="Space B", created_by_member_id=owner_id))
            await session.flush()
            session.add_all(
                [
                    SpaceMembershipModel(
                        id=uuid4(), space_id="space-a", member_id=owner_id, role="owner"
                    ),
                    SpaceMembershipModel(
                        id=uuid4(), space_id="space-b", member_id=owner_id, role="owner"
                    ),
                ]
            )
            website_session = await SessionService(session).issue(
                session_principal(owner_id),
                now=now,
                step_up_at=now,
            )
            created = await APIKeyService(session, codec).create(
                owner_id,
                family_id=website_session.family_id,
                name="RLS key",
                scopes={"knowledge:read"},
                space_grants={"space-a"},
                request_id="rls-create",
                now=now,
            )
            raw_key = created.secret.reveal()

        async with factory.begin() as session:
            resolved = await APIKeyService(session, codec).resolve(raw_key, now=now)

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "SELECT set_config('app.principal_id', :member_id, true), "
                    "set_config('app.principal_restricted', 'false', true), "
                    "set_config('app.credential_id', :credential_id, true)"
                ),
                {"member_id": str(owner_id), "credential_id": str(created.key_id)},
            )
            visible_a = await connection.scalar(
                text("SELECT gateway_has_space_role('space-a', ARRAY['owner','editor','reader'])")
            )
            visible_b = await connection.scalar(
                text("SELECT gateway_has_space_role('space-b', ARRAY['owner','editor','reader'])")
            )
            assert resolved.space_grants == frozenset({"space-a"})
            assert visible_a is True
            assert visible_b is False
            grant_rows = list(
                await connection.execute(
                    text("SELECT space_id FROM api_key_space_grants WHERE api_key_id = :key_id"),
                    {"key_id": created.key_id},
                )
            )
            assert [row[0] for row in grant_rows] == ["space-a"]
