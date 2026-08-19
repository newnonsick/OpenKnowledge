

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Optional

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.gateway.config import get_settings
from src.gateway.infrastructure.database import normalize_database_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchemaStatus:
    current_revision: Optional[str]
    head_revisions: tuple[str, ...]
    compatible: bool
    embedding_dimensions: tuple[int, ...] = ()

def get_alembic_config(db_url: Optional[str] = None) -> Config:

    base_dir = Path(__file__).resolve().parent.parent.parent.parent
    alembic_ini = base_dir / "alembic.ini"

    if not alembic_ini.exists():
        alembic_ini = Path.cwd() / "alembic.ini"

    if not alembic_ini.exists():
        raise FileNotFoundError(f"alembic.ini not found at {alembic_ini} or {base_dir}")

    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(base_dir / "alembic"))

    target_url = normalize_database_url(db_url or get_settings().database.url)
    cfg.set_main_option("sqlalchemy.url", target_url)
    return cfg

def run_migrations_sync(alembic_cfg: Optional[Config] = None) -> None:

    cfg = alembic_cfg or get_alembic_config()
    logger.info("Executing Alembic upgrade head...")
    command.upgrade(cfg, "head")
    logger.info("Alembic upgrade head completed successfully.")

async def run_migrations_async(db_url: Optional[str] = None) -> None:

    cfg = get_alembic_config(db_url)
    await asyncio.to_thread(run_migrations_sync, cfg)


async def get_schema_status_async(
    db_url: Optional[str] = None,
    expected_embedding_dimension: Optional[int] = None,
) -> SchemaStatus:
    cfg = get_alembic_config(db_url)
    heads = tuple(ScriptDirectory.from_config(cfg).get_heads())
    expected_dimension = expected_embedding_dimension or get_settings().embedding.dimension
    engine = create_async_engine(
        normalize_database_url(db_url or get_settings().database.url),
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            current = await connection.run_sync(
                lambda sync_connection: MigrationContext.configure(
                    sync_connection
                ).get_current_revision()
            )
            dimension_rows = await connection.execute(
                text(
                    "SELECT format_type(a.atttypid, a.atttypmod) "
                    "FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname IN ('knowledge_revisions', 'document_chunks') "
                    "AND a.attname = 'embedding' "
                    "AND n.nspname = current_schema() "
                    "ORDER BY c.relname"
                )
            )
            dimensions = tuple(
                int(match.group(1))
                for value in dimension_rows.scalars().all()
                if (match := re.fullmatch(r"vector\((\d+)\)", value or ""))
            )
    finally:
        await engine.dispose()
    revision_compatible = current is not None and current in heads
    dimension_compatible = (
        len(dimensions) == 2
        and all(dimension == expected_dimension for dimension in dimensions)
    )
    return SchemaStatus(
        current_revision=current,
        head_revisions=heads,
        compatible=revision_compatible and dimension_compatible,
        embedding_dimensions=dimensions,
    )

def rollback_migrations_sync(revision: str = "base", alembic_cfg: Optional[Config] = None) -> None:

    cfg = alembic_cfg or get_alembic_config()
    logger.info(f"Executing Alembic downgrade to {revision}...")
    command.downgrade(cfg, revision)
    logger.info(f"Alembic downgrade to {revision} completed successfully.")

async def rollback_migrations_async(revision: str = "base", db_url: Optional[str] = None) -> None:

    cfg = get_alembic_config(db_url)
    await asyncio.to_thread(rollback_migrations_sync, revision, cfg)
