

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any, Optional

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import column, func, select, table, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.gateway.config import get_settings
from src.gateway.infrastructure.database import normalize_database_url

logger = logging.getLogger(__name__)

VECTOR_COLUMN = "embedding"
VECTOR_INDEXES: dict[str, str] = {
    "document_chunks": "ix_document_chunks_embedding",
    "knowledge_revisions": "ix_knowledge_revisions_embedding",
    "retrieval_units": "ix_retrieval_units_embedding",
}
HNSW_INDEX_OPTIONS = "WITH (m = 16, ef_construction = 64)"

_EMBEDDING_DIMENSION_SQL = (
    "SELECT c.relname, format_type(a.atttypid, a.atttypmod) "
    "FROM pg_attribute a "
    "JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = ANY(:tables) "
    "AND a.attname = :column "
    "AND n.nspname = current_schema() "
    "AND NOT a.attisdropped "
    "ORDER BY c.relname"
)


@dataclass(frozen=True)
class SchemaStatus:
    current_revision: Optional[str]
    head_revisions: tuple[str, ...]
    compatible: bool
    embedding_dimensions: tuple[int, ...] = ()
    vector_extension_version: Optional[str] = None
    trigram_extension_available: bool = False


@dataclass(frozen=True)
class EmbeddingDimensionStatus:
    configured: int
    current: dict[str, Optional[int]]
    stored: dict[str, int]
    cleared: int = 0

    @property
    def aligned(self) -> bool:
        return bool(self.current) and all(
            dimension == self.configured for dimension in self.current.values()
        )

    @property
    def stored_total(self) -> int:
        return sum(self.stored.values())


def _version_tuple(value: Optional[str]) -> tuple[int, ...]:
    if not value:
        return ()
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?", value)
    if match is None:
        return ()
    return tuple(int(part or 0) for part in match.groups())


def _parse_vector_dimension(value: Optional[str]) -> Optional[int]:
    match = re.fullmatch(r"(?:vector|halfvec)\((\d+)\)", value or "")
    return int(match.group(1)) if match else None


def _vector_tables() -> list[str]:
    return sorted(VECTOR_INDEXES)


async def _read_embedding_dimensions(connection: Any) -> dict[str, Optional[int]]:
    rows = (
        await connection.execute(
            text(_EMBEDDING_DIMENSION_SQL),
            {"tables": _vector_tables(), "column": VECTOR_COLUMN},
        )
    ).tuples().all()
    return {name: _parse_vector_dimension(formatted) for name, formatted in rows}


async def _read_embedding_column_types(connection: Any) -> dict[str, str]:
    rows = (
        await connection.execute(
            text(_EMBEDDING_DIMENSION_SQL),
            {"tables": _vector_tables(), "column": VECTOR_COLUMN},
        )
    ).tuples().all()
    return {name: formatted or "" for name, formatted in rows}


async def _count_stored_embeddings(connection: Any) -> dict[str, int]:
    stored: dict[str, int] = {}
    for name in _vector_tables():
        count = await connection.scalar(
            select(func.count())
            .select_from(table(name))
            .where(column(VECTOR_COLUMN).is_not(None))
        )
        stored[name] = int(count or 0)
    return stored


def _privileged_database_url(db_url: Optional[str] = None) -> str:
    database = get_settings().database
    return normalize_database_url(db_url or database.migration_url or database.url)


def get_alembic_config(db_url: Optional[str] = None) -> Config:

    base_dir = Path(__file__).resolve().parent.parent.parent.parent
    alembic_ini = base_dir / "alembic.ini"

    if not alembic_ini.exists():
        alembic_ini = Path.cwd() / "alembic.ini"

    if not alembic_ini.exists():
        raise FileNotFoundError(f"alembic.ini not found at {alembic_ini} or {base_dir}")

    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(base_dir / "alembic"))

    database_settings = get_settings().database
    target_url = normalize_database_url(
        db_url or database_settings.migration_url or database_settings.url
    )
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
            dimension_map = await _read_embedding_dimensions(connection)
            dimensions = tuple(
                dimension for dimension in dimension_map.values() if dimension is not None
            )
            extension_result = await connection.execute(
                text(
                    "SELECT extname, extversion FROM pg_extension "
                    "WHERE extname IN ('vector', 'pg_trgm')"
                )
            )
            extension_rows = dict(extension_result.tuples().all())
    finally:
        await engine.dispose()
    revision_compatible = current is not None and current in heads
    dimension_compatible = (
        len(dimensions) == len(VECTOR_INDEXES)
        and all(dimension == expected_dimension for dimension in dimensions)
    )
    vector_version = extension_rows.get("vector")
    extensions_compatible = (
        _version_tuple(vector_version) >= (0, 8, 0)
        and "pg_trgm" in extension_rows
    )
    return SchemaStatus(
        current_revision=current,
        head_revisions=heads,
        compatible=revision_compatible and dimension_compatible and extensions_compatible,
        embedding_dimensions=dimensions,
        vector_extension_version=vector_version,
        trigram_extension_available="pg_trgm" in extension_rows,
    )

def rollback_migrations_sync(revision: str = "base", alembic_cfg: Optional[Config] = None) -> None:

    cfg = alembic_cfg or get_alembic_config()
    logger.info(f"Executing Alembic downgrade to {revision}...")
    command.downgrade(cfg, revision)
    logger.info(f"Alembic downgrade to {revision} completed successfully.")

async def rollback_migrations_async(revision: str = "base", db_url: Optional[str] = None) -> None:

    cfg = get_alembic_config(db_url)
    await asyncio.to_thread(rollback_migrations_sync, revision, cfg)


async def get_embedding_dimension_status(
    db_url: Optional[str] = None,
    *,
    count_stored: bool = True,
) -> EmbeddingDimensionStatus:
    engine = create_async_engine(_privileged_database_url(db_url), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            current = await _read_embedding_dimensions(connection)
            stored = await _count_stored_embeddings(connection) if count_stored else {}
    finally:
        await engine.dispose()
    return EmbeddingDimensionStatus(
        configured=get_settings().embedding.dimension,
        current=current,
        stored=stored,
    )


async def _stored_embedding_counts(db_url: Optional[str] = None) -> dict[str, int]:
    engine = create_async_engine(_privileged_database_url(db_url), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await _count_stored_embeddings(connection)
    finally:
        await engine.dispose()


async def set_embedding_dimension(
    dimension: int,
    *,
    allow_embedding_loss: bool = False,
    db_url: Optional[str] = None,
) -> EmbeddingDimensionStatus:
    target = int(dimension)
    if target <= 0 or target > 4000:
        raise RuntimeError(
            f"Embedding dimension must be between 1 and 4000 for halfvec HNSW index, got {dimension}."
        )

    status = await get_schema_status_async(db_url, expected_embedding_dimension=target)
    if status.current_revision not in status.head_revisions:
        raise RuntimeError(
            f"Database schema is at revision {status.current_revision or 'unversioned'} "
            f"instead of {', '.join(status.head_revisions)}; "
            "run 'python -m src.gateway.cli migrate' first."
        )

    pending = await get_embedding_dimension_status(db_url, count_stored=False)
    if not pending.current:
        raise RuntimeError("No pgvector embedding columns were found in the current schema.")

    engine = create_async_engine(_privileged_database_url(db_url), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            current_types = await _read_embedding_column_types(connection)
    finally:
        await engine.dispose()

    if (
        all(existing == target for existing in pending.current.values())
        and all(col_type == f"halfvec({target})" for col_type in current_types.values())
    ):
        return pending

    stored = await _stored_embedding_counts(db_url)
    cleared = sum(stored.values())
    if cleared and not allow_embedding_loss:
        populated = ", ".join(
            f"{name}={count}" for name, count in sorted(stored.items()) if count
        )
        raise RuntimeError(
            f"Resizing clears {cleared} stored embeddings ({populated}). "
            "Re-run with --allow-embedding-loss once they have been re-ingested or are expendable."
        )

    logger.info(f"Resizing pgvector embedding columns to {target} dimensions.")
    engine = create_async_engine(_privileged_database_url(db_url), poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            for name, index_name in sorted(VECTOR_INDEXES.items()):
                await connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
                await connection.execute(
                    text(
                        f"ALTER TABLE {name} ALTER COLUMN {VECTOR_COLUMN} "
                        f"TYPE halfvec({target}) USING NULL"
                    )
                )
                await connection.execute(
                    text(
                        f"CREATE INDEX {index_name} ON {name} "
                        f"USING hnsw ({VECTOR_COLUMN} halfvec_cosine_ops) {HNSW_INDEX_OPTIONS}"
                    )
                )
    except Exception as exc:
        raise RuntimeError(f"Failed to resize embedding columns: {exc}") from exc
    finally:
        await engine.dispose()
    resized = await get_embedding_dimension_status(db_url, count_stored=False)
    return EmbeddingDimensionStatus(
        configured=resized.configured,
        current=resized.current,
        stored=resized.stored,
        cleared=cleared,
    )
