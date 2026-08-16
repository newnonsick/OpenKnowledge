"""Tier 1 Feature Tests for Feature 4: Automatic DB Migrations on Startup.

Validates Alembic configuration loader, filesystem script artifacts, URL normalization,
and automated migration execution lifecycle.
"""

from pathlib import Path
import pytest

from src.gateway.infrastructure.database import normalize_database_url
from src.gateway.infrastructure.migrations import get_alembic_config
from tests.e2e.harness.test_env import TestEnvironment, clean_database_tables


@pytest.mark.tier1
@pytest.mark.feature("F4")
def test_f04_alembic_config_resolution():
    """Verify get_alembic_config locates alembic.ini and sets main options."""
    cfg = get_alembic_config("postgresql+asyncpg://user:pass@localhost:5432/testdb")
    assert cfg is not None
    assert cfg.get_main_option("script_location") is not None
    assert "testdb" in cfg.get_main_option("sqlalchemy.url")


@pytest.mark.tier1
@pytest.mark.feature("F4")
def test_f04_alembic_filesystem_artifacts():
    """Verify that alembic.ini, alembic directory, and script templates exist."""
    root = Path(".")
    alembic_ini = root / "alembic.ini"
    assert alembic_ini.exists(), "alembic.ini must exist in project root"

    alembic_dir = root / "alembic"
    assert alembic_dir.is_dir(), "alembic directory must exist"

    env_py = alembic_dir / "env.py"
    assert env_py.exists(), "alembic/env.py must exist"

    versions_dir = alembic_dir / "versions"
    assert versions_dir.exists(), "alembic/versions directory must exist"


@pytest.mark.tier1
@pytest.mark.feature("F4")
def test_f04_database_url_normalization():
    """Verify normalize_database_url converts standard postgres schemes to asyncpg."""
    assert normalize_database_url("postgresql://user:pw@host/db") == "postgresql+asyncpg://user:pw@host/db"
    assert normalize_database_url("postgres://user:pw@host/db") == "postgresql+asyncpg://user:pw@host/db"
    assert normalize_database_url("postgresql+asyncpg://user:pw@host/db") == "postgresql+asyncpg://user:pw@host/db"
    assert normalize_database_url("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"


@pytest.mark.tier1
@pytest.mark.feature("F4")
@pytest.mark.asyncio
async def test_f04_startup_lifespan_migration_trigger():
    """Verify that TestEnvironment boots schema and creates all tables cleanly."""
    async with TestEnvironment() as env:
        assert env.engine is not None
        # Clean execution confirms tables are present
        await env.clean_database()


@pytest.mark.tier1
@pytest.mark.feature("F4")
@pytest.mark.asyncio
async def test_f04_clean_database_tables_utility():
    """Verify clean_database_tables helper truncates tables and re-seeds global workspace."""
    async with TestEnvironment() as env:
        # Verify cleaning truncates tables and seeds global workspace
        await clean_database_tables(env.engine, env.is_postgres)
        async with env.session_factory() as session:
            from sqlalchemy import select
            from src.gateway.infrastructure.persistence.models import Workspace
            stmt = select(Workspace).where(Workspace.id == "global")
            result = await session.execute(stmt)
            ws = result.scalar_one_or_none()
            assert ws is not None
            assert ws.id == "global"
