"""Global pytest fixtures and configuration for unit and integration testing."""

import os
from pathlib import Path
from typing import Generator
import pytest

from src.gateway.config import Settings
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter


@pytest.fixture
def temp_storage_dir(tmp_path: Path) -> Path:
    """Fixture providing an isolated temporary storage directory."""
    storage_dir = tmp_path / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    return storage_dir


@pytest.fixture
def local_storage(temp_storage_dir: Path) -> LocalStorageAdapter:
    """Fixture providing a LocalStorageAdapter instance pointing to temporary directory."""
    return LocalStorageAdapter(base_dir=temp_storage_dir)


@pytest.fixture
def test_settings(temp_storage_dir: Path) -> Settings:
    """Fixture providing pre-configured test settings."""
    return Settings(
        llm={
            "url": "http://localhost:8888",
            "model_id": "test-model",
            "api_key": "test-llm-key",
            "context_window": 8192,
        },
        embedding={
            "url": "http://localhost:7997",
            "model_id": "test-embed-model",
            "api_key": "test-embed-key",
            "dimension": 768,
        },
        database={
            "url": "postgresql+asyncpg://postgres:postgres@localhost:5432/gateway_test",
        },
        gateway={
            "api_keys": ["test-key-1", "test-key-2"],
            "storage_dir": str(temp_storage_dir),
            "log_level": "DEBUG",
            "max_tool_iterations": 10,
        },
    )
