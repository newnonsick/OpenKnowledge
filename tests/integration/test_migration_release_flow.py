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
    rollback_migrations_async,
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
        assert status.current_revision == "019"
        assert status.embedding_dimensions == (1024, 1024)
        assert status.vector_extension_version is not None
        assert status.trigram_extension_available is True

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
                assert await connection.scalar(
                    text(
                        "SELECT to_regprocedure("
                        "'gateway_reject_archived_document_provenance()')"
                    )
                ) is not None
                trigger_contract = (
                    await connection.execute(
                        text(
                            "SELECT trigger.tgenabled::text AS tgenabled, trigger.tgtype "
                            "FROM pg_trigger trigger "
                            "JOIN pg_class relation ON relation.oid = trigger.tgrelid "
                            "WHERE relation.relname = 'provenance_links' "
                            "AND trigger.tgname = 'trg_provenance_reject_archived_document'"
                        )
                    )
                ).one()
                assert trigger_contract.tgenabled == "O"
                assert trigger_contract.tgtype == 23
                assert await connection.scalar(
                    text(
                        "SELECT pg_has_role(current_user, 'gateway_maintenance', 'MEMBER')"
                    )
                )
        finally:
            await isolated_engine.dispose()

        await rollback_migrations_async("017", isolated_url)
        rolled_back_engine = create_async_engine(isolated_url, poolclass=NullPool)
        try:
            async with rolled_back_engine.connect() as connection:
                assert await connection.scalar(
                    text(
                        "SELECT to_regprocedure("
                        "'gateway_run_retention(timestamp with time zone,timestamp with time zone,"
                        "timestamp with time zone,integer)')"
                    )
                ) is None
                assert await connection.scalar(
                    text(
                        "SELECT to_regprocedure("
                        "'gateway_reject_archived_document_provenance()')"
                    )
                ) is None
        finally:
            await rolled_back_engine.dispose()
        await run_migrations_async(isolated_url)
        assert (
            await get_schema_status_async(
                isolated_url,
                expected_embedding_dimension=1024,
            )
        ).current_revision == "019"
    finally:
        if created:
            async with admin_engine.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
                )
        await admin_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_non_superuser_migration_owner_can_upgrade_downgrade_and_upgrade_again():
    base_url = get_settings().database.url
    admin_engine = create_async_engine(base_url, poolclass=NullPool)
    database_name = validate_database_name(isolated_database_name())
    migration_role = f"akg_migrator_{uuid4().hex[:12]}"
    migration_password = uuid4().hex
    assert re.fullmatch(r"akg_migrator_[0-9a-f]{12}", migration_role)
    role_created = False
    database_created = False
    try:
        async with admin_engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            can_administer = await connection.scalar(
                text(
                    "SELECT rolsuper OR rolcreaterole FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
            if not can_administer:
                pytest.skip("Configured PostgreSQL role cannot provision a migration owner")
            await connection.execute(
                text(
                    f'CREATE ROLE "{migration_role}" LOGIN PASSWORD \'{migration_password}\' '
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS"
                )
            )
            role_created = True
            await connection.execute(
                text(
                    f'GRANT gateway_maintenance TO "{migration_role}" WITH ADMIN OPTION'
                )
            )
            await connection.execute(text(f'GRANT "{migration_role}" TO CURRENT_USER'))
            await connection.execute(
                text(
                    f'CREATE DATABASE "{database_name}" OWNER "{migration_role}" TEMPLATE template0'
                )
            )
            database_created = True
            await connection.execute(text(f'REVOKE "{migration_role}" FROM CURRENT_USER'))

        admin_isolated_url = make_url(base_url).set(database=database_name).render_as_string(hide_password=False)
        admin_isolated_engine = create_async_engine(admin_isolated_url, poolclass=NullPool)
        try:
            async with admin_isolated_engine.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        finally:
            await admin_isolated_engine.dispose()

        base_database_url = make_url(base_url)
        tenant_suffix = ""
        if base_database_url.username and "." in base_database_url.username:
            tenant_suffix = base_database_url.username[base_database_url.username.index(".") :]
        migration_url = (
            base_database_url
            .set(
                username=migration_role + tenant_suffix,
                password=migration_password,
                database=database_name,
            )
            .render_as_string(hide_password=False)
        )
        await run_migrations_async(migration_url)
        status = await get_schema_status_async(migration_url, expected_embedding_dimension=1024)
        assert status.current_revision == "019"
        migration_engine = create_async_engine(migration_url, poolclass=NullPool)
        try:
            async with migration_engine.connect() as connection:
                role_contract = (
                    await connection.execute(
                        text(
                            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                            "FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).one()
                assert not any(role_contract)
                assert await connection.scalar(
                    text("SELECT pg_has_role(current_user, 'gateway_maintenance', 'MEMBER')")
                )
        finally:
            await migration_engine.dispose()

        await rollback_migrations_async("017", migration_url)
        await run_migrations_async(migration_url)
        assert (
            await get_schema_status_async(migration_url, expected_embedding_dimension=1024)
        ).current_revision == "019"
    finally:
        async with admin_engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            if role_created:
                await connection.execute(text(f'GRANT "{migration_role}" TO CURRENT_USER'))
            if database_created:
                await connection.execute(
                    text(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
                )
            if role_created:
                await connection.execute(text(f'REVOKE "{migration_role}" FROM CURRENT_USER'))
                await connection.execute(text(f'DROP ROLE IF EXISTS "{migration_role}"'))
        await admin_engine.dispose()
