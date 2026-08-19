import asyncio
import re
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.database import get_db_session, set_session_factory, validate_runtime_database_connection
from src.gateway.infrastructure.persistence.knowledge_repository import KnowledgeRepository
from src.gateway.infrastructure.persistence.principal_context import bind_principal, reset_principal
from src.gateway.infrastructure.persistence.principal_context import set_principal_context
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_bound_request_principal_is_installed_transaction_locally() -> None:
    member_id = uuid4()
    request_principal = Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
    )

    async with isolated_postgres_database() as (_, factory):
        set_session_factory(factory)
        token = bind_principal(request_principal)
        try:
            dependency = get_db_session()
            session = await anext(dependency)
            assert await session.scalar(
                text("SELECT current_setting('app.principal_id', true)")
            ) == str(member_id)
            with pytest.raises(StopAsyncIteration):
                await anext(dependency)
        finally:
            reset_principal(token)
            set_session_factory(None)


async def test_forced_rls_fails_closed_and_transaction_context_does_not_leak() -> None:
    runtime_role = f"akg_runtime_{uuid4().hex[:12]}"
    assert re.fullmatch(r"akg_runtime_[0-9a-f]{12}", runtime_role)
    owner_id = uuid4()
    other_id = uuid4()
    disabled_id = uuid4()
    super_admin_id = uuid4()
    race_owner_a_id = uuid4()
    race_owner_b_id = uuid4()
    own_item = uuid4()
    reader_item = uuid4()
    hidden_item = uuid4()
    created_space_id = f"created-{uuid4().hex[:12]}"

    async with isolated_postgres_database() as (engine, _):
        try:
            async with engine.begin() as connection:
                await connection.execute(text(f'CREATE ROLE "{runtime_role}" NOLOGIN NOBYPASSRLS'))
                await connection.execute(text(f'GRANT "{runtime_role}" TO CURRENT_USER'))
                await connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{runtime_role}"'))
                await connection.execute(
                    text(
                        "GRANT SELECT ON alembic_version, api_key_scopes, audit_events, "
                        "compatibility_principals, document_chunks, document_files, "
                        "idempotency_records, knowledge_items, knowledge_revisions, "
                        "login_throttle_buckets, members, mfa_factors, mfa_recovery_codes, "
                        "password_credentials, personal_api_keys, session_credentials, "
                        f'session_families, space_memberships, workspaces TO "{runtime_role}"'
                    )
                )
                await connection.execute(
                    text(
                        "GRANT INSERT ON api_key_scopes, audit_events, "
                        "document_chunks, document_files, idempotency_records, knowledge_items, "
                        "knowledge_revisions, login_throttle_buckets, members, mfa_factors, "
                        "mfa_recovery_codes, password_credentials, personal_api_keys, "
                        "session_credentials, session_families, space_memberships, workspaces "
                        f'TO "{runtime_role}"'
                    )
                )
                await connection.execute(
                    text(
                        "GRANT UPDATE ON document_chunks, document_files, "
                        "idempotency_records, knowledge_items, knowledge_revisions, "
                        "login_throttle_buckets, members, mfa_factors, mfa_recovery_codes, "
                        "password_credentials, personal_api_keys, session_credentials, session_families "
                        f'TO "{runtime_role}"'
                    )
                )
                await connection.execute(
                    text(
                        "GRANT DELETE ON api_key_scopes, document_chunks, document_files, "
                        f'knowledge_items, knowledge_revisions, space_memberships TO "{runtime_role}"'
                    )
                )
                await connection.execute(
                    text(f'GRANT UPDATE (revoked_at) ON compatibility_principals TO "{runtime_role}"')
                )
                await connection.execute(
                    text(f'GRANT UPDATE (role, updated_at) ON space_memberships TO "{runtime_role}"')
                )
                await connection.execute(
                    text(f'GRANT UPDATE (name, archived_at, revision) ON workspaces TO "{runtime_role}"')
                )
                await connection.execute(
                    text(f'GRANT USAGE, SELECT ON login_throttle_buckets_id_seq TO "{runtime_role}"')
                )
                await connection.execute(
                    text(
                        "GRANT EXECUTE ON FUNCTION gateway_actor_active(), "
                        "gateway_has_space_role(text, text[]), "
                        "gateway_is_initial_space_owner(text, uuid), "
                        "gateway_can_change_membership(text, uuid, text) "
                        f'TO "{runtime_role}"'
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO members "
                        "(id, username, username_normalized, display_name, status, system_role, force_password_change) VALUES "
                        "(:owner, 'owner', 'owner', 'Owner', 'active', 'member', false), "
                        "(:other, 'other', 'other', 'Other', 'active', 'member', false), "
                        "(:disabled, 'disabled', 'disabled', 'Disabled', 'disabled', 'member', false), "
                        "(:admin, 'admin', 'admin', 'Admin', 'active', 'super_admin', false), "
                        "(:race_a, 'race-a', 'race-a', 'Race A', 'active', 'member', false), "
                        "(:race_b, 'race-b', 'race-b', 'Race B', 'active', 'member', false)"
                    ),
                    {
                        "owner": owner_id,
                        "other": other_id,
                        "disabled": disabled_id,
                        "admin": super_admin_id,
                        "race_a": race_owner_a_id,
                        "race_b": race_owner_b_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name) VALUES "
                        "('own-space', 'Own'), ('reader-space', 'Reader'), "
                        "('hidden-space', 'Hidden'), ('race-space', 'Race')"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO space_memberships (id, space_id, member_id, role) VALUES "
                        "(gen_random_uuid(), 'own-space', :owner, 'owner'), "
                        "(gen_random_uuid(), 'reader-space', :owner, 'reader'), "
                        "(gen_random_uuid(), 'reader-space', :disabled, 'reader'), "
                        "(gen_random_uuid(), 'hidden-space', :other, 'owner'), "
                        "(gen_random_uuid(), 'race-space', :race_a, 'owner'), "
                        "(gen_random_uuid(), 'race-space', :race_b, 'owner')"
                    ),
                    {
                        "owner": owner_id,
                        "disabled": disabled_id,
                        "other": other_id,
                        "race_a": race_owner_a_id,
                        "race_b": race_owner_b_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO knowledge_items "
                        "(id, workspace_id, title, content, is_global, is_deleted) VALUES "
                        "(:own, 'own-space', 'Own', 'own', false, false), "
                        "(:reader, 'reader-space', 'Reader', 'reader', false, false), "
                        "(:hidden, 'hidden-space', 'Hidden', 'hidden', false, false)"
                    ),
                    {"own": own_item, "reader": reader_item, "hidden": hidden_item},
                )

            async with engine.connect() as connection:
                await connection.execute(text(f'SET ROLE "{runtime_role}"'))
                await connection.commit()
                await validate_runtime_database_connection(connection)
                await connection.commit()

                async def delete_racing_owner(member_id) -> int:
                    async with engine.connect() as racing_connection:
                        await racing_connection.execute(
                            text(f'SET ROLE "{runtime_role}"')
                        )
                        await racing_connection.commit()
                        async with racing_connection.begin():
                            await racing_connection.execute(
                                text(
                                    "SELECT set_config("
                                    "'app.principal_id', :member_id, true)"
                                ),
                                {"member_id": str(member_id)},
                            )
                            result = await racing_connection.execute(
                                text(
                                    "DELETE FROM space_memberships "
                                    "WHERE space_id = 'race-space' "
                                    "AND member_id = :member_id"
                                ),
                                {"member_id": member_id},
                            )
                            return result.rowcount

                deleted_owner_counts = await asyncio.gather(
                    delete_racing_owner(race_owner_a_id),
                    delete_racing_owner(race_owner_b_id),
                )
                assert sorted(deleted_owner_counts) == [0, 1]

                async with connection.begin():
                    assert await connection.scalar(text("SELECT count(*) FROM knowledge_items")) == 0
                    assert await connection.scalar(text("SELECT count(*) FROM workspaces")) == 0
                    assert await connection.scalar(text("SELECT count(*) FROM space_memberships")) == 0
                    await connection.execute(text("SELECT set_config('app.principal_id', 'not-a-uuid', true)"))
                    assert await connection.scalar(text("SELECT count(*) FROM knowledge_items")) == 0

                runtime_factory = async_sessionmaker(
                    bind=connection,
                    expire_on_commit=False,
                )
                repository = KnowledgeRepository(session_factory=runtime_factory)
                assert await repository.get_item_by_id(own_item) is None
                request_principal = Principal(
                    subject_id=str(owner_id),
                    kind=PrincipalKind.API_KEY,
                    system_role=SystemRole.MEMBER,
                    scopes=frozenset({"knowledge:read", "knowledge:write"}),
                )
                token = bind_principal(request_principal)
                try:
                    assert (
                        await repository.get_item_by_id(own_item)
                    ).title == "Own"
                    repository_item_id = uuid4()
                    repository_revision = KnowledgeRevision(
                        id=uuid4(),
                        item_id=repository_item_id,
                        version=1,
                        content="created through repository",
                        content_hash=KnowledgeRevision.compute_hash(
                            "created through repository"
                        ),
                    )
                    created_item = await repository.create_item(
                        KnowledgeItem(
                            id=repository_item_id,
                            workspace_id="own-space",
                            title="Repository",
                            content="created through repository",
                        ),
                        repository_revision,
                    )
                    assert created_item.id == repository_item_id
                finally:
                    reset_principal(token)
                restricted_token = bind_principal(
                    Principal(
                        subject_id=str(owner_id),
                        kind=PrincipalKind.SESSION,
                        system_role=SystemRole.MEMBER,
                        scopes=frozenset({"knowledge:read"}),
                        restricted=True,
                    )
                )
                try:
                    assert await repository.get_item_by_id(own_item) is None
                finally:
                    reset_principal(restricted_token)

                async with connection.begin():
                    await connection.execute(
                        text("SELECT set_config('app.principal_id', :member_id, true)"),
                        {"member_id": str(owner_id)},
                    )
                    titles = tuple(
                        await connection.scalars(
                            text("SELECT title FROM knowledge_items ORDER BY title")
                        )
                    )
                    assert titles == ("Own", "Reader", "Repository")
                    assert tuple(
                        await connection.scalars(text("SELECT id FROM workspaces ORDER BY id"))
                    ) == ("own-space", "reader-space")
                    assert await connection.scalar(
                        text("SELECT count(*) FROM space_memberships")
                    ) == 3
                    await connection.execute(
                        text(
                            "INSERT INTO workspaces (id, name, created_by_member_id) "
                            "VALUES (:space_id, 'Created through runtime role', :owner)"
                        ),
                        {"space_id": created_space_id, "owner": owner_id},
                    )
                    with pytest.raises(DBAPIError):
                        async with connection.begin_nested():
                            await connection.execute(
                                text(
                                    "INSERT INTO space_memberships "
                                    "(id, space_id, member_id, role) "
                                    "VALUES (gen_random_uuid(), :space_id, :owner, 'reader')"
                                ),
                                {"space_id": created_space_id, "owner": owner_id},
                            )
                    await connection.execute(
                        text(
                            "INSERT INTO space_memberships "
                            "(id, space_id, member_id, role) "
                            "VALUES (gen_random_uuid(), :space_id, :owner, 'owner')"
                        ),
                        {"space_id": created_space_id, "owner": owner_id},
                    )
                    demoted = await connection.execute(
                        text(
                            "UPDATE space_memberships SET role = 'reader' "
                            "WHERE space_id = :space_id AND member_id = :owner"
                        ),
                        {"space_id": created_space_id, "owner": owner_id},
                    )
                    assert demoted.rowcount == 0
                    deleted = await connection.execute(
                        text(
                            "DELETE FROM space_memberships "
                            "WHERE space_id = :space_id AND member_id = :owner"
                        ),
                        {"space_id": created_space_id, "owner": owner_id},
                    )
                    assert deleted.rowcount == 0
                    await connection.execute(
                        text(
                            "INSERT INTO knowledge_items "
                            "(id, workspace_id, title, content, is_global, is_deleted) "
                            "VALUES (gen_random_uuid(), 'own-space', 'Allowed', 'allowed', false, false)"
                        )
                    )
                    with pytest.raises(DBAPIError):
                        async with connection.begin_nested():
                            await connection.execute(
                                text(
                                    "UPDATE knowledge_items SET content = 'denied' "
                                    "WHERE id = :item_id"
                                ),
                                {"item_id": reader_item},
                            )

                async with connection.begin():
                    assert await connection.scalar(text("SELECT count(*) FROM knowledge_items")) == 0
                    assert await connection.scalar(text("SELECT count(*) FROM workspaces")) == 0
                    await set_principal_context(
                        connection,
                        Principal(
                            subject_id=str(super_admin_id),
                            kind=PrincipalKind.SESSION,
                            system_role=SystemRole.SUPER_ADMIN,
                            scopes=frozenset({"*"}),
                        ),
                    )
                    assert await connection.scalar(text("SELECT count(*) FROM knowledge_items")) == 0

                async with connection.begin():
                    await connection.execute(
                        text("SELECT set_config('app.principal_id', :member_id, true)"),
                        {"member_id": str(disabled_id)},
                    )
                    assert await connection.scalar(text("SELECT count(*) FROM knowledge_items")) == 0

                await connection.execute(text("RESET ROLE"))
                await connection.commit()
                with pytest.raises(RuntimeError, match="row-security contract"):
                    await validate_runtime_database_connection(connection)
                await connection.rollback()
                await connection.execute(text(f'SET ROLE "{runtime_role}"'))
                await connection.commit()
                async with connection.begin():
                    await connection.execute(
                        text("SELECT set_config('app.principal_id', :member_id, true)"),
                        {"member_id": str(owner_id)},
                    )
                    await connection.execute(
                        text("UPDATE workspaces SET archived_at = now() WHERE id = 'own-space'")
                    )
                async with connection.begin():
                    await connection.execute(
                        text("SELECT set_config('app.principal_id', :member_id, true)"),
                        {"member_id": str(owner_id)},
                    )
                    assert tuple(
                        await connection.scalars(
                            text("SELECT title FROM knowledge_items ORDER BY title")
                        )
                    ) == ("Reader",)

                await connection.execute(text("RESET ROLE"))
                await connection.commit()
        finally:
            async with engine.begin() as connection:
                await connection.execute(text("RESET ROLE"))
                await connection.execute(text(f'DROP OWNED BY "{runtime_role}"'))
                await connection.execute(text(f'DROP ROLE IF EXISTS "{runtime_role}"'))
