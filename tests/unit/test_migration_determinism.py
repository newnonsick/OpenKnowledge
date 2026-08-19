from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from src.gateway.config import Settings
from src.gateway.infrastructure.migrations import SchemaStatus


ROOT = Path(__file__).resolve().parents[2]


def render_sql(command: str, dimension: int, workspace_id: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": "postgresql+asyncpg://user:password@localhost/gateway",
            "EMBEDDING_DIMENSION": str(dimension),
            "DEFAULT_WORKSPACE_ID": workspace_id,
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *command.split(), "--sql"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_offline_upgrade_sql_is_independent_of_runtime_configuration():
    first = render_sql("upgrade head", 384, "workspace-one")
    second = render_sql("upgrade head", 1536, "workspace-two")

    assert first == second
    assert "VECTOR(1024)" in first
    assert "workspace_id = 'global'" in first


def test_downgrade_keeps_infrastructure_owned_vector_extension():
    sql = render_sql("downgrade 001:base", 1024, "global")

    assert "DROP EXTENSION" not in sql.upper()


def test_cli_check_is_safe_and_returns_success(monkeypatch, capsys):
    from src.gateway import cli

    sentinel = "sentinel-database-password"
    status = SchemaStatus(current_revision="002", head_revisions=("002",), compatible=True)
    check = AsyncMock(return_value=status)
    monkeypatch.setattr(cli, "get_schema_status_async", check)
    monkeypatch.setenv(
        "DATABASE_URL",
        f"postgresql+asyncpg://admin:{sentinel}@private-db/gateway",
    )

    assert cli.main(["check"]) == 0
    output = capsys.readouterr().out
    assert "compatible" in output.lower()
    assert sentinel not in output


def test_cli_check_returns_nonzero_for_incompatible_schema(monkeypatch):
    from src.gateway import cli

    status = SchemaStatus(current_revision="001", head_revisions=("002",), compatible=False)
    monkeypatch.setattr(cli, "get_schema_status_async", AsyncMock(return_value=status))

    assert cli.main(["check"]) == 1


def test_cli_migrate_invokes_explicit_upgrade(monkeypatch):
    from src.gateway import cli

    migrate = AsyncMock()
    monkeypatch.setattr(cli, "run_migrations_async", migrate)

    assert cli.main(["migrate"]) == 0
    migrate.assert_awaited_once()


@pytest.mark.asyncio
async def test_web_lifespan_checks_schema_without_running_migrations(monkeypatch):
    from src.gateway import main

    app = FastAPI()
    app.state.settings = Settings(gateway={"environment": "test"})
    status = SchemaStatus(current_revision="002", head_revisions=("002",), compatible=True)
    schema_check = AsyncMock(return_value=status)
    migrate = AsyncMock()
    bootstrap = AsyncMock()
    monkeypatch.setattr(main, "get_schema_status_async", schema_check, raising=False)
    monkeypatch.setattr(main, "run_migrations_async", migrate, raising=False)
    monkeypatch.setattr(main, "bootstrap_global_workspace", bootstrap)
    monkeypatch.setattr(main, "close_db_engine", AsyncMock())
    monkeypatch.setattr(main.HttpLLMClient, "close_shared_client", AsyncMock())
    monkeypatch.setattr(main.HTTPEmbeddingClient, "close_shared_client", AsyncMock())

    async with main.lifespan(app):
        assert app.state.schema_compatible is True

    migrate.assert_not_awaited()
    schema_check.assert_awaited_once()
    bootstrap.assert_awaited_once_with(app.state.settings)
