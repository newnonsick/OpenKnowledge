from uuid import uuid4

import pytest
from sqlalchemy import text

from src.gateway.infrastructure.migrations import get_schema_status_async, rollback_migrations_async, run_migrations_async
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_identity_status_contract_migrates_existing_rows_both_directions() -> None:
    async with isolated_postgres_database() as (engine, _):
        database_url = engine.url.render_as_string(hide_password=False)
        await engine.dispose()
        await rollback_migrations_async("003", database_url)

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO members "
                    "(id, username, username_normalized, display_name, status, system_role, force_password_change) "
                    "VALUES (gen_random_uuid(), 'legacy', 'legacy', 'Legacy', 'invited', 'member', true)"
                )
            )

        await engine.dispose()
        await run_migrations_async(database_url)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT status FROM members WHERE username = 'legacy'")) == "pending"
            key_version = await connection.scalar(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'mfa_factors' AND column_name = 'encryption_key_version'"
                )
            )
            assert key_version == "NO"

        await engine.dispose()
        await rollback_migrations_async("003", database_url)
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT status FROM members WHERE username = 'legacy'")) == "invited"


async def test_api_key_pepper_downgrade_refuses_incompatible_credentials() -> None:
    member_id = uuid4()
    key_id = uuid4()
    async with isolated_postgres_database() as (engine, _):
        database_url = engine.url.render_as_string(hide_password=False)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO members "
                    "(id, username, username_normalized, display_name, status, system_role, force_password_change) "
                    "VALUES (:member_id, 'pepper-owner', 'pepper-owner', 'Pepper Owner', 'active', 'member', false)"
                ),
                {"member_id": member_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO personal_api_keys "
                    "(id, member_id, public_id, key_digest, pepper_version, name, status) "
                    "VALUES (:key_id, :member_id, 'version-two-key', :digest, 2, 'Test', 'active')"
                ),
                {"key_id": key_id, "member_id": member_id, "digest": "a" * 64},
            )
        await engine.dispose()
        with pytest.raises(RuntimeError, match="non-v1 pepper"):
            await rollback_migrations_async("005", database_url)
        status = await get_schema_status_async(database_url)
        assert status.current_revision == "007"
