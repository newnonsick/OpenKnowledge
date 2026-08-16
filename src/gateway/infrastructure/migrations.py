

import asyncio
import logging
from pathlib import Path
from typing import Optional

from alembic import command
from alembic.config import Config

from src.gateway.config import get_settings
from src.gateway.infrastructure.database import normalize_database_url

logger = logging.getLogger(__name__)

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

def rollback_migrations_sync(revision: str = "base", alembic_cfg: Optional[Config] = None) -> None:

    cfg = alembic_cfg or get_alembic_config()
    logger.info(f"Executing Alembic downgrade to {revision}...")
    command.downgrade(cfg, revision)
    logger.info(f"Alembic downgrade to {revision} completed successfully.")

async def rollback_migrations_async(revision: str = "base", db_url: Optional[str] = None) -> None:

    cfg = get_alembic_config(db_url)
    await asyncio.to_thread(rollback_migrations_sync, revision, cfg)
