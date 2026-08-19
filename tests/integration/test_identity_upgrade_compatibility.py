from sqlalchemy import text

from src.gateway.infrastructure.migrations import rollback_migrations_async, run_migrations_async
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
