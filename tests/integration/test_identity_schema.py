from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.gateway.config import get_settings
from src.gateway.infrastructure.database import normalize_database_url
from src.gateway.infrastructure.migrations import get_schema_status_async, run_migrations_async


async def test_configured_postgres_has_identity_invariants() -> None:
    await run_migrations_async()
    status = await get_schema_status_async(expected_embedding_dimension=1024)
    assert status.compatible is True
    assert status.current_revision == "020"

    engine = create_async_engine(
        normalize_database_url(get_settings().database.url),
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = current_schema()"
                        )
                    )
                ).scalars()
            )
            assert {
                "members",
                "space_memberships",
                "personal_api_keys",
                "audit_events",
            } <= tables
            global_name = await connection.scalar(
                text("SELECT name FROM workspaces WHERE id = 'global'")
            )
            assert global_name == "Family Shared"

            constraints = set(
                (
                    await connection.execute(
                        text(
                            "SELECT conname FROM pg_constraint c "
                            "JOIN pg_namespace n ON n.oid = c.connamespace "
                            "WHERE n.nspname = current_schema()"
                        )
                    )
                ).scalars()
            )
            assert "uq_members_username_normalized" in constraints
            assert "uq_space_memberships_space_member" in constraints
            assert "ck_members_system_role" in constraints
    finally:
        await engine.dispose()
