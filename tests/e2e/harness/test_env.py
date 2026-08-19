"""
Standalone Test Environment Harness for Local AI Gateway E2E Tests.

Provides:
- TestEnvironment context manager
- Database engine lifecycle and SQLite fallback type compilers (Vector, TSVECTOR, UUID)
- Environment variable isolation and settings cache clearing
- Isolated temporary filesystem storage management
- SSE Stream parsing utility
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID as PG_UUID
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import Computed
from pgvector.sqlalchemy import Vector


# -----------------------------------------------------------------------------
# 1. SQLite SQLAlchemy Type Compilers (Graceful Fallback Mode)
# -----------------------------------------------------------------------------

@compiles(Vector, "sqlite")
def _compile_vector_sqlite(type_, compiler, **kw):
    return "TEXT"


@compiles(TSVECTOR, "sqlite")
def _compile_tsvector_sqlite(type_, compiler, **kw):
    return "TEXT"


@compiles(PG_UUID, "sqlite")
def _compile_pg_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "TEXT"


@compiles(Computed, "sqlite")
def _compile_computed_sqlite(element, compiler, **kw):
    """
    Render Computed columns as empty in SQLite fallback mode to avoid
    evaluating Postgres-specific functions (e.g. to_tsvector).
    """
    return ""


# -----------------------------------------------------------------------------
# 1b. SQLite DDL Compiler Hooks for Postgres Dialect Adaptations
# -----------------------------------------------------------------------------

_orig_sqlite_render_default_string = SQLiteDDLCompiler.render_default_string


def _sqlite_render_default_string(self, default):
    res = _orig_sqlite_render_default_string(self, default)
    if res and "::" in res:
        # Strip PostgreSQL type casting (e.g., '::jsonb') for SQLite DDL compatibility
        res = re.sub(r"::[a-zA-Z0-9_]+", "", res)
    return res


SQLiteDDLCompiler.render_default_string = _sqlite_render_default_string


# -----------------------------------------------------------------------------
# 2. Database Detection & Lifecycle Utilities
# -----------------------------------------------------------------------------

def configured_database_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        os.environ.get(
            "DATABASE_URL",
            os.environ.get(
                "DB_URL",
                "postgresql+asyncpg://postgres:postgres@localhost:5432/gateway_test_db",
            ),
        ),
    )


async def detect_database_configuration() -> Tuple[str, bool]:
    """
    Detect whether PostgreSQL is available or if SQLite fallback must be used.
    Returns (db_url, is_postgres).
    """
    explicit_url = (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("DB_URL")
    )
    pg_url = configured_database_url()
    if pg_url.startswith("postgresql"):
        test_engine = None
        try:
            test_engine = create_async_engine(pg_url, connect_args={"timeout": 2.0})
            async with test_engine.connect() as conn:
                await conn.execute(text("SELECT 1;"))
            return pg_url, True
        except Exception as exc:
            if explicit_url:
                raise RuntimeError("Configured PostgreSQL is unavailable") from exc
        finally:
            if test_engine is not None:
                dispose_result = test_engine.dispose()
                if inspect.isawaitable(dispose_result):
                    await dispose_result

    return "sqlite+aiosqlite:///:memory:", False


def validate_test_schema_name(schema_name: str) -> str:
    if not re.fullmatch(r"gateway_test_[0-9a-f]{16,32}", schema_name):
        raise ValueError(f"Unsafe PostgreSQL test schema name: {schema_name}")
    return schema_name


async def clean_database_tables(engine: AsyncEngine, is_postgres: bool) -> None:
    """Clean all tables and re-seed the default global workspace."""
    async with engine.begin() as conn:
        if is_postgres:
            try:
                await conn.execute(
                    text(
                        "TRUNCATE TABLE knowledge_revisions, knowledge_items, document_chunks, document_files, workspaces CASCADE;"
                    )
                )
                await conn.execute(
                    text(
                        "INSERT INTO workspaces (id, name) VALUES ('global', 'Global Shared Workspace') ON CONFLICT DO NOTHING;"
                    )
                )
            except Exception:
                pass
        else:
            try:
                await conn.execute(text("DELETE FROM knowledge_revisions;"))
                await conn.execute(text("DELETE FROM knowledge_items;"))
                await conn.execute(text("DELETE FROM document_chunks;"))
                await conn.execute(text("DELETE FROM document_files;"))
                await conn.execute(text("DELETE FROM workspaces WHERE id != 'global';"))
                await conn.execute(
                    text(
                        "INSERT OR IGNORE INTO workspaces (id, name) VALUES ('global', 'Global Shared Workspace');"
                    )
                )
            except Exception:
                pass


# -----------------------------------------------------------------------------
# 3. TestEnvironment Class
# -----------------------------------------------------------------------------

class TestEnvironment:
    __test__ = False
    """
    Comprehensive Test Environment encapsulating settings overrides,
    temporary file storage, database engine, mock server coordination,
    and FastAPI lifespan execution.
    """

    def __init__(
        self,
        env_overrides: Optional[Dict[str, str]] = None,
        db_url: Optional[str] = None,
        storage_dir: Optional[Path] = None,
    ):
        self.custom_env_overrides = env_overrides or {}
        self.custom_db_url = db_url
        self.custom_storage_dir = storage_dir

        self.temp_dir: Optional[tempfile.TemporaryDirectory] = None
        self.storage_path: Optional[Path] = None
        self.db_url: Optional[str] = None
        self.is_postgres: bool = False
        self.engine: Optional[AsyncEngine] = None
        self.session_factory: Optional[async_sessionmaker[AsyncSession]] = None
        self.postgres_schema: Optional[str] = None
        self._orig_environ: Dict[str, str] = {}

    async def __aenter__(self) -> TestEnvironment:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start and initialize the entire test environment."""
        # 1. Setup temporary storage directory
        if self.custom_storage_dir:
            self.storage_path = self.custom_storage_dir
            self.storage_path.mkdir(parents=True, exist_ok=True)
        else:
            self.temp_dir = tempfile.TemporaryDirectory(prefix="gateway_test_storage_")
            self.storage_path = Path(self.temp_dir.name)

        # 2. Resolve database URL
        if self.custom_db_url:
            self.db_url = self.custom_db_url
            self.is_postgres = self.db_url.startswith("postgresql")
        else:
            self.db_url, self.is_postgres = await detect_database_configuration()

        from src.gateway.infrastructure.persistence.models import EMBED_DIM

        # 3. Build test environment variables
        test_env = {
            "HOST": "127.0.0.1",
            "ENVIRONMENT": "test",
            "PORT": "8000",
            "LOG_LEVEL": "DEBUG",
            "STORAGE_DIR": str(self.storage_path),
            "DEFAULT_WORKSPACE_ID": "global",
            "GATEWAY_API_KEYS": "sk-test-admin,sk-test-user-1,sk-test-user-2",
            "LEGACY_API_KEYS_ENABLED": "true",
            "CORS_ORIGINS": "*",
            "TRUSTED_HOSTS": "localhost,127.0.0.1,[::1],testserver,test,gateway-test",
            "MAX_TOOL_ITERATIONS": "5",
            "TOOL_TIMEOUT_SECONDS": "5.0",
            "LLM_URL": "http://mock-llm.test/v1",
            "LLM_MODEL_ID": "mock-llama-3.1-8b",
            "LLM_API_KEY": "mock-llm-key",
            "LLM_CONTEXT_WINDOW": "4096",
            "LLM_TIMEOUT_SECONDS": "10.0",
            "LLM_TEMPERATURE": "0.7",
            "EMBEDDING_URL": "http://mock-embedding.test/v1",
            "EMBEDDING_MODEL_ID": "mock-bge-large",
            "EMBEDDING_API_KEY": "mock-embed-key",
            "EMBEDDING_DIMENSION": str(EMBED_DIM),
            "EMBEDDING_BATCH_SIZE": "16",
            "EMBEDDING_TIMEOUT_SECONDS": "5.0",
            "DB_URL": self.db_url,
            "DB_POOL_SIZE": "5",
            "DB_MAX_OVERFLOW": "5",
            "DB_POOL_TIMEOUT": "10.0",
            "DB_ECHO": "false",
        }
        test_env.update(self.custom_env_overrides)

        # 4. Patch os.environ
        self._orig_environ = os.environ.copy()
        for k, v in test_env.items():
            os.environ[k] = str(v)

        # 5. Clear cached pydantic settings
        try:
            from src.gateway.config import get_settings
            get_settings.cache_clear()
        except ImportError:
            try:
                from gateway.config import get_settings
                get_settings.cache_clear()
            except ImportError:
                pass

        # 6. Initialize database engine
        if self.is_postgres:
            self.postgres_schema = validate_test_schema_name(
                f"gateway_test_{secrets.token_hex(16)}"
            )
            bootstrap_engine = create_async_engine(self.db_url)
            try:
                async with bootstrap_engine.begin() as conn:
                    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                    await conn.execute(text(f"CREATE SCHEMA {self.postgres_schema};"))
            finally:
                await bootstrap_engine.dispose()
            self.engine = create_async_engine(
                self.db_url,
                pool_size=5,
                max_overflow=5,
                connect_args={
                    "server_settings": {
                        "search_path": f"{self.postgres_schema},public"
                    }
                },
            )
        else:
            self.engine = create_async_engine(
                self.db_url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
            # Enforce foreign keys on SQLite so the test suite catches FK ordering
            # bugs that PostgreSQL would surface in production.
            from sqlalchemy import event

            @event.listens_for(self.engine.sync_engine, "connect")
            def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):  # noqa: ANN001
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        try:
            from src.gateway.infrastructure.database import set_session_factory
            set_session_factory(self.session_factory)
        except Exception:
            pass

        # 7. Create database schema
        from src.gateway.infrastructure.persistence.identity_models import (
            CompatibilityPrincipalModel,
            MemberModel,
            SpaceMembershipModel,
        )
        from src.gateway.infrastructure.persistence.models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=False)
            )
            # Seed global workspace
            await conn.execute(
                text(
                    "INSERT INTO workspaces (id, name) VALUES "
                    "('global', 'Global Shared Workspace'), "
                    "('test_ws', 'Test Workspace'), "
                    "('ws_hybrid', 'Hybrid Workspace'), "
                    "('ws-backend', 'Backend Workspace'), "
                    "('team_a', 'Team A'), "
                    "('team_b', 'Team B') ON CONFLICT DO NOTHING;"
                )
                if self.is_postgres
                else text(
                    "INSERT OR IGNORE INTO workspaces (id, name) VALUES "
                    "('global', 'Global Shared Workspace'), "
                    "('test_ws', 'Test Workspace'), "
                    "('ws_hybrid', 'Hybrid Workspace'), "
                    "('ws-backend', 'Backend Workspace'), "
                    "('team_a', 'Team A'), "
                    "('team_b', 'Team B');"
                )
            )
        workspace_ids = (
            "global",
            "test_ws",
            "ws_hybrid",
            "ws-backend",
            "team_a",
            "team_b",
        )
        configured_keys = tuple(
            key.strip()
            for key in test_env["GATEWAY_API_KEYS"].split(",")
            if key.strip()
        )
        async with self.session_factory.begin() as session:
            for index, key in enumerate(configured_keys, start=1):
                key_digest = hashlib.sha256(key.encode("utf-8")).digest()
                principal_id = UUID(bytes=key_digest[:16])
                session.add(
                    MemberModel(
                        id=principal_id,
                        username=f"legacy-test-{index}",
                        username_normalized=f"legacy-test-{index}",
                        display_name=f"Legacy Test {index}",
                        status="active",
                        system_role="member",
                        force_password_change=False,
                    )
                )
                session.add(
                    CompatibilityPrincipalModel(
                        id=principal_id,
                        name=f"legacy-test-{index}",
                        key_digest=key_digest.hex(),
                        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
                    )
                )
                session.add_all(
                    SpaceMembershipModel(
                        id=uuid4(),
                        space_id=workspace_id,
                        member_id=principal_id,
                        role="editor",
                    )
                    for workspace_id in workspace_ids
                )

    async def stop(self) -> None:
        """Tear down test environment, restore settings, and clean storage."""
        try:
            from src.gateway.infrastructure.database import set_session_factory
            set_session_factory(None)
        except Exception:
            pass

        if self.engine:
            await self.engine.dispose()
            self.engine = None

        if self.is_postgres and self.postgres_schema and self.db_url:
            schema_name = validate_test_schema_name(self.postgres_schema)
            cleanup_engine = create_async_engine(self.db_url)
            try:
                async with cleanup_engine.begin() as conn:
                    await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE;"))
            finally:
                await cleanup_engine.dispose()
            self.postgres_schema = None

        if self.temp_dir:
            self.temp_dir.cleanup()
            self.temp_dir = None

        # Restore original environment
        os.environ.clear()
        os.environ.update(self._orig_environ)

        try:
            from src.gateway.config import get_settings
            get_settings.cache_clear()
        except ImportError:
            try:
                from gateway.config import get_settings
                get_settings.cache_clear()
            except ImportError:
                pass

    async def clean_database(self) -> None:
        """Truncates data tables and re-seeds global workspace between tests."""
        if self.engine:
            await clean_database_tables(self.engine, self.is_postgres)

    def get_client(self, api_key: Optional[str] = "sk-test-admin") -> httpx.AsyncClient:
        """Create an AsyncClient with ASGITransport bound to the gateway app."""
        try:
            from src.gateway.main import create_app
        except ImportError:
            from gateway.main import create_app

        app = create_app()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers=headers,
        )


# -----------------------------------------------------------------------------
# 3b. Gateway wired to a real-socket mock upstream (full-stack HTTP harness)
# -----------------------------------------------------------------------------

class GatewayMockUpstream:
    """Gateway HTTP harness wired to an in-process mock LLM/embedding upstream.

    Starts the mock upstream on an ephemeral OS socket, then boots a
    TestEnvironment whose LLM/embedding settings point at it, so requests
    issued through ``self.client`` traverse the full gateway stack (auth
    middleware, protocol converters, orchestrator, HTTP adapters) before
    reaching the mock. Use this instead of posting to ``mock_mgr.app``
    directly: the mock app only tests the mock, not the gateway.
    """

    def __init__(
        self,
        embedding_dimension: int = 1024,
        env_overrides: Optional[Dict[str, str]] = None,
        api_key: Optional[str] = "sk-test-admin",
    ):
        self._dimension = embedding_dimension
        self._overrides = dict(env_overrides or {})
        self._api_key = api_key
        self.mock: Optional[Any] = None
        self.env: Optional[TestEnvironment] = None
        self.client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "GatewayMockUpstream":
        from tests.e2e.harness.mock_server import MockServerManager

        self.mock = MockServerManager(embedding_dimension=self._dimension)
        await self.mock.start()

        overrides = {
            "LLM_URL": self.mock.llm_url,
            "LLM_API_KEY": "mock-llm-key",
            "EMBEDDING_URL": self.mock.embedding_url,
            "EMBEDDING_MODEL_ID": "mock-bge-large",
            "EMBEDDING_API_KEY": "mock-embed-key",
            "EMBEDDING_DIMENSION": str(self._dimension),
            "EMBEDDING_BATCH_SIZE": "32",
        }
        overrides.update(self._overrides)

        self.env = TestEnvironment(env_overrides=overrides)
        await self.env.start()
        self.client = self.env.get_client(api_key=self._api_key)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if self.env is not None:
                await self.env.stop()
        finally:
            if self.mock is not None:
                await self.mock.stop()
            self.mock = None
            self.env = None
            self.client = None

    @property
    def llm(self):
        """Mock LLM controller (queue responses, inspect recorded requests)."""
        assert self.mock is not None, "GatewayMockUpstream not started"
        return self.mock.llm

    @property
    def embedding(self):
        """Mock embedding controller."""
        assert self.mock is not None, "GatewayMockUpstream not started"
        return self.mock.embedding


# -----------------------------------------------------------------------------
# 4. SSE Stream Helper Utility
# -----------------------------------------------------------------------------

async def parse_sse_stream(response: httpx.Response) -> List[Dict[str, Any]]:
    """
    Parse an SSE response stream into a list of parsed JSON data payloads.
    Stops upon encountering '[DONE]'.
    """
    events = []
    async for line in response.aiter_lines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue  # Comment or keepalive line
        if line.startswith("data: "):
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                events.append({"raw": payload})
    return events
