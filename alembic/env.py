"""Alembic async environment runner."""

import asyncio
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

from src.gateway.config import settings
from src.gateway.infrastructure.database import normalize_database_url
from src.gateway.infrastructure.persistence.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers must stay False: this runs during FastAPI startup,
    # and the default (True) silences uvicorn's and the app's loggers, hiding
    # startup failures behind a bare "Waiting for application startup."
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    raw_url = config.get_main_option("sqlalchemy.url", settings.database.url)
    url = normalize_database_url(raw_url)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using AsyncEngine."""
    configuration = config.get_section(config.config_ini_section, {})
    raw_url = config.get_main_option("sqlalchemy.url", settings.database.url)
    configuration["sqlalchemy.url"] = normalize_database_url(raw_url)

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entrypoint for online migrations."""
    # If connection was passed in config.attributes (e.g. testing)
    connectable = config.attributes.get("connection", None)
    if connectable is not None:
        if isinstance(connectable, Connection):
            do_run_migrations(connectable)
        else:
            raise TypeError("Expected sqlalchemy.engine.Connection")
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
