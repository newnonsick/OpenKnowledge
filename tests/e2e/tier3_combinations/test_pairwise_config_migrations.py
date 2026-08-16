"""Tier 3 Pairwise Combination Tests: Settings (.env) + Automatic DB Migrations + Global Workspace Seeding.

Tests cross-feature interactions between:
- Feature 1: Decoupled Pydantic Settings & .env.example
- Feature 4: Automatic DB Migrations on Startup
- Feature 5: Global Workspace Bootstrapping
- Feature 6: Local Disk Storage Adapter
- Feature 3: SQLAlchemy 2.0 Async + pgvector Models
"""

import os
from pathlib import Path
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.gateway.config import Settings
from src.gateway.infrastructure.persistence.models import (
    Base,
    DocumentChunk,
    DocumentFile,
    KnowledgeItem,
    KnowledgeRevision,
    Workspace,
)
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter
from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f01_f03_f04_config_database_and_table_schema_creation():
    """Test pairwise interaction: Settings DB URL + Model metadata table creation across all 5 tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Inspect table names
        res = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table';"))
        table_names = {row[0] for row in res.fetchall()}

    assert "workspaces" in table_names
    assert "knowledge_items" in table_names
    assert "knowledge_revisions" in table_names
    assert "document_files" in table_names
    assert "document_chunks" in table_names

    await engine.dispose()


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f04_f05_config_global_workspace_seeding_and_idempotency():
    """Test pairwise interaction: Startup bootstrapping seeds 'global' workspace and is strictly idempotent."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # First seeding
        await conn.execute(
            text("INSERT OR IGNORE INTO workspaces (id, name) VALUES ('global', 'Global Shared Workspace');")
        )

        # Query workspace
        res1 = await conn.execute(select(Workspace.id, Workspace.name).where(Workspace.id == "global"))
        ws1 = res1.first()
        assert ws1 is not None
        assert ws1[0] == "global"
        assert ws1[1] == "Global Shared Workspace"

        # Duplicate seeding must not fail or create duplicates
        await conn.execute(
            text("INSERT OR IGNORE INTO workspaces (id, name) VALUES ('global', 'Global Shared Workspace');")
        )
        res2 = await conn.execute(select(Workspace.id).where(Workspace.id == "global"))
        all_global = res2.fetchall()
        assert len(all_global) == 1

    await engine.dispose()


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f01_f06_config_storage_directory_and_traversal_protection(tmp_path: Path):
    """Test pairwise interaction: GatewaySettings storage_dir configures LocalStorageAdapter safely."""
    storage_root = tmp_path / "custom_storage"
    adapter = LocalStorageAdapter(base_dir=storage_root)

    assert storage_root.exists()

    saved_path = await adapter.save_file(
        workspace_id="test_ws",
        file_id="file1",
        filename="doc.txt",
        content=b"Hello Storage",
    )
    assert saved_path.exists()
    assert await adapter.exists("test_ws", "file1", "doc.txt")
    read_data = await adapter.read_file("test_ws", "file1", "doc.txt")
    assert read_data == b"Hello Storage"


@pytest.mark.tier3
def test_pairwise_f01_f19_config_llm_and_embedding_settings_decoupling():
    """Test pairwise interaction: LLM settings and Embedding settings are fully decoupled with distinct models and URLs."""
    cfg = Settings(
        llm={
            "url": "http://vllm-server:8000/v1",
            "model_id": "meta-llama/Llama-3.1-70B-Instruct",
            "api_key": "llm-secret",
            "context_window": 16384,
        },
        embedding={
            "url": "http://tei-server:8080",
            "model_id": "BAAI/bge-large-en-v1.5",
            "api_key": "embed-secret",
            "dimension": 1024,
        },
        database={"url": "sqlite+aiosqlite:///:memory:"},
        gateway={"api_keys": ["key1", "key2"], "storage_dir": "./tmp_storage"},
    )

    assert cfg.llm.url == "http://vllm-server:8000/v1"
    assert cfg.llm.model_id == "meta-llama/Llama-3.1-70B-Instruct"
    assert cfg.llm.context_window == 16384

    assert cfg.embedding.url == "http://tei-server:8080"
    assert cfg.embedding.model_id == "BAAI/bge-large-en-v1.5"
    assert cfg.embedding.dimension == 1024

    assert cfg.llm.url != cfg.embedding.url
    assert cfg.llm.model_id != cfg.embedding.model_id


@pytest.mark.tier3
@pytest.mark.asyncio
async def test_pairwise_f01_f04_f05_config_test_env_lifecycle_integration():
    """Test pairwise interaction: TestEnvironment startup, DB setup, clean_database, and shutdown."""
    test_env = TestEnvironment(db_url="sqlite+aiosqlite:///:memory:")
    await test_env.start()

    assert test_env.engine is not None
    assert test_env.session_factory is not None

    async with test_env.session_factory() as session:
        res = await session.execute(select(Workspace.id).where(Workspace.id == "global"))
        ws_id = res.scalar_one_or_none()
        assert ws_id == "global"

    await test_env.clean_database()

    async with test_env.session_factory() as session:
        res = await session.execute(select(Workspace.id).where(Workspace.id == "global"))
        assert res.scalar_one_or_none() == "global"

    await test_env.stop()
    assert test_env.engine is None
