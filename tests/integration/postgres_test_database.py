from contextlib import asynccontextmanager
import re
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.gateway.config import get_settings
from src.gateway.infrastructure.migrations import run_migrations_async


@asynccontextmanager
async def isolated_postgres_database():
    base_url = get_settings().database.url
    admin = create_async_engine(base_url, poolclass=NullPool)
    name = f"akg_test_{uuid4().hex[:16]}"
    assert re.fullmatch(r"akg_test_[0-9a-f]{16}", name)
    created = False
    try:
        async with admin.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            can_create = await connection.scalar(text("SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user"))
            if not can_create:
                pytest.skip("Configured PostgreSQL role cannot create an isolated test database")
            await connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE template0'))
            created = True
        url = make_url(base_url).set(database=name).render_as_string(hide_password=False)
        await run_migrations_async(url)
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            yield engine, async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
    finally:
        if created:
            async with admin.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()
