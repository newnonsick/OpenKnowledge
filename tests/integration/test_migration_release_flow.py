from __future__ import annotations

import re
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.gateway.config import get_settings
from src.gateway.infrastructure.migrations import (
    get_schema_status_async,
    run_migrations_async,
)


def isolated_database_name() -> str:
    return f"akg_test_{uuid4().hex[:16]}"


def validate_database_name(name: str) -> str:
    if not re.fullmatch(r"akg_test_[0-9a-f]{16}", name):
        raise ValueError("Unsafe integration database name")
    return name


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fresh_database_upgrade_and_schema_check():
    base_url = get_settings().database.url
    admin_engine = create_async_engine(base_url, poolclass=NullPool)
    database_name = validate_database_name(isolated_database_name())
    created = False

    try:
        async with admin_engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            can_create = await connection.scalar(
                text(
                    "SELECT rolcreatedb OR rolsuper FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
            if not can_create:
                pytest.skip("Configured PostgreSQL role cannot create an isolated test database")
            await connection.execute(text(f'CREATE DATABASE "{database_name}" TEMPLATE template0'))
            created = True

        isolated_url = make_url(base_url).set(database=database_name).render_as_string(hide_password=False)
        await run_migrations_async(isolated_url)
        status = await get_schema_status_async(isolated_url, expected_embedding_dimension=1024)

        assert status.compatible is True
        assert status.current_revision == "014"
        assert status.embedding_dimensions == (1024, 1024)

        mismatched_status = await get_schema_status_async(
            isolated_url,
            expected_embedding_dimension=768,
        )
        assert mismatched_status.compatible is False

        isolated_engine = create_async_engine(isolated_url, poolclass=NullPool)
        try:
            async with isolated_engine.connect() as connection:
                vector_version = await connection.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
                vector_type = await connection.scalar(
                    text(
                        "SELECT format_type(a.atttypid, a.atttypmod) "
                        "FROM pg_attribute a "
                        "JOIN pg_class c ON c.oid = a.attrelid "
                        "WHERE c.relname = 'knowledge_revisions' "
                        "AND a.attname = 'embedding'"
                    )
                )
                assert vector_version is not None
                assert vector_type == "vector(1024)"
        finally:
            await isolated_engine.dispose()
    finally:
        if created:
            async with admin_engine.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
                )
        await admin_engine.dispose()
