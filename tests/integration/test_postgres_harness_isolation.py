from __future__ import annotations

import pytest
from sqlalchemy import text

from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sequential_postgres_environments_use_isolated_schemas():
    first_schema = None

    async with TestEnvironment() as first:
        if not first.is_postgres:
            pytest.skip("PostgreSQL test database is not configured")
        first_schema = first.postgres_schema
        assert first_schema is not None
        async with first.engine.begin() as conn:
            assert await conn.scalar(text("SELECT current_schema();")) == first_schema
            await conn.execute(
                text(
                    "INSERT INTO workspaces (id, name) "
                    "VALUES ('isolation-probe', 'Isolation Probe');"
                )
            )

    async with TestEnvironment() as second:
        assert second.is_postgres
        assert second.postgres_schema is not None
        assert second.postgres_schema != first_schema
        async with second.engine.begin() as conn:
            assert await conn.scalar(text("SELECT current_schema();")) == second.postgres_schema
            assert await conn.scalar(
                text("SELECT count(*) FROM workspaces WHERE id = 'isolation-probe';")
            ) == 0
            assert await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_namespace "
                    "WHERE nspname = :schema_name;"
                ),
                {"schema_name": first_schema},
            ) == 0
