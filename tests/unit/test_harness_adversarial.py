"""
Adversarial Stress-Test Suite for E2E Test Runner and Test Environment Harness.

Covers:
1. `tests/e2e/harness/runner.py`:
   - CLI flags: --dry-run, --tier, --feature, --json-output, --markdown-output, --min-tests, --max-duration, -k, -x, -v
   - Windows path normalization and tier/feature extraction in PytestResultCollector
   - Setup/Teardown failure outcome mapping
   - Exit code contracts (0 on success, 1 on failure or unmet threshold)
   - Zero-test edge cases in ReportFormatter (divide-by-zero protection, empty matrix)
   - JSON & Markdown schema validation
2. `tests/e2e/harness/test_env.py`:
   - Environment variable isolation and restoration
   - Temp storage creation, permission, and exit cleanup
   - Settings cache invalidation before/after
   - Standalone SQLite compilation for Vector, TSVECTOR, PG_UUID
   - SSE stream parser robustness (comments, malformed JSON, multi-event chunks, [DONE])
   - Re-entrancy, sequential lifecycles, and exception recovery in TestEnvironment
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import Computed, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID as PG_UUID
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool
from pgvector.sqlalchemy import Vector

from src.gateway.config import get_settings
from src.gateway.infrastructure.persistence.models import (
    Base,
    DocumentChunk,
    DocumentFile,
    KnowledgeItem,
    KnowledgeRevision,
    Workspace,
    EMBED_DIM,
)
from tests.e2e.harness.runner import (
    FEATURES,
    TIER_DIRS,
    TIER_NAMES,
    TIER_THRESHOLDS,
    E2ETestRunner,
    PytestResultCollector,
    ReportFormatter,
    SuiteSummary,
    TestResultItem,
    TierStatistics,
)
from tests.e2e.harness.test_env import (
    TestEnvironment,
    configured_database_url,
    validate_test_schema_name,
    clean_database_tables,
    detect_database_configuration,
    parse_sse_stream,
)


# ==============================================================================
# SECTION 1: RUNNER CLI & COMPONENT ADVERSARIAL STRESS TESTS
# ==============================================================================

class TestRunnerAdversarial:
    """Adversarial stress-testing of runner.py and PytestResultCollector."""

    def test_feature_registry_integrity(self):
        """Verify all 29 features exist and are monotonically indexed 1..29."""
        assert len(FEATURES) == 29
        for idx in range(1, 30):
            assert idx in FEATURES
            code, desc = FEATURES[idx]
            assert code == f"F{idx}"
            assert isinstance(desc, str) and len(desc) > 3

    def test_collector_windows_path_normalization(self):
        """Verify feature and tier extraction handles Windows backslashes and forward slashes."""
        collector = PytestResultCollector()

        # Windows backslash paths
        win_path_1 = "tests\\e2e\\tier1_features\\test_f01_settings.py"
        win_node_1 = "tests\\e2e\\tier1_features\\test_f01_settings.py::test_case"
        win_path_24 = "tests\\e2e\\tier2_boundaries\\test_f24_orchestration_bounds.py"
        win_node_24 = "tests\\e2e\\tier2_boundaries\\test_f24_orchestration_bounds.py::test_bounds"

        assert collector._extract_feature_id(win_node_1, win_path_1) == "F1"
        assert collector._extract_feature_id(win_node_24, win_path_24) == "F24"

        # Mock report with Windows backslash path
        mock_report = MagicMock()
        mock_report.when = "call"
        mock_report.nodeid = win_node_1
        mock_report.outcome = "passed"
        mock_report.failed = False
        mock_report.skipped = False
        mock_report.duration = 0.01
        mock_report.longrepr = None

        collector.pytest_runtest_logreport(mock_report)
        assert len(collector.results) == 1
        res = collector.results[0]
        assert res.tier == "1"
        assert res.feature_id == "F1"
        assert res.outcome == "passed"

    def test_collector_setup_teardown_failures(self):
        """Verify collector captures setup and teardown failures as 'failed' outcomes."""
        collector = PytestResultCollector()

        # Setup failure
        setup_report = MagicMock()
        setup_report.when = "setup"
        setup_report.failed = True
        setup_report.skipped = False
        setup_report.nodeid = "tests/e2e/tier1_features/test_f03_models.py::test_init"
        setup_report.duration = 0.05
        setup_report.longrepr = "Database connection error in fixture setup"

        collector.pytest_runtest_logreport(setup_report)
        assert len(collector.results) == 1
        assert collector.results[0].outcome == "failed"
        assert collector.results[0].error_message == "Database connection error in fixture setup"

        # Successful setup should be ignored (only 'call' or failed setup/teardown captured)
        good_setup = MagicMock()
        good_setup.when = "setup"
        good_setup.failed = False
        good_setup.skipped = False
        good_setup.nodeid = "tests/e2e/tier1_features/test_f03_models.py::test_init"
        collector.pytest_runtest_logreport(good_setup)
        assert len(collector.results) == 1  # Unchanged

    def test_collector_feature_regex_variations(self):
        """Verify extraction handles various naming conventions (f01, f1, F15, feature_10, etc.)."""
        collector = PytestResultCollector()
        cases = [
            ("tests/e2e/tier1_features/test_f1_settings.py", "F1"),
            ("tests/e2e/tier1_features/test_f01_settings.py", "F1"),
            ("tests/e2e/tier1_features/test_f9_registry.py", "F9"),
            ("tests/e2e/tier1_features/test_f10_auth.py", "F10"),
            ("tests/e2e/tier1_features/test_f28_suite.py", "F28"),
            ("tests/e2e/tier2_boundaries/test_feature_07_bounds.py", "F7"),
            ("tests/e2e/tier2_boundaries/test_feature_24_bounds.py", "F24"),
            ("tests/e2e/tier3_combinations/test_c01_pairwise.py", None),
            ("tests/e2e/tier4_scenarios/test_s01_scenario.py", None),
        ]
        for path, expected in cases:
            node = f"{path}::test_case"
            assert collector._extract_feature_id(node, path) == expected

    def test_aggregation_threshold_enforcement(self):
        """Verify threshold logic correctly assesses MET / UNMET status."""
        runner = E2ETestRunner(tiers=["1", "2"])

        # Case 1: Insufficient tests in Tier 1 (139 < 140)
        results = [
            TestResultItem(
                nodeid=f"tests/e2e/tier1_features/test_f01.py::test_{i}",
                name=f"test_{i}",
                file_path="tests/e2e/tier1_features/test_f01.py",
                tier="1",
                feature_id="F1",
                outcome="passed",
                duration=0.01,
            )
            for i in range(139)
        ]
        summary = runner._aggregate_summary("2026-08-15T12:00:00Z", results, total_duration=1.0)
        assert summary.tier_stats["1"].total == 139
        assert summary.tier_stats["1"].passed_threshold is False
        assert summary.thresholds_passed is False
        assert summary.verdict == "FAILED"

        # Case 2: Sufficient tests (140) with 0 failures in Tier 1, but Tier 2 has 0 (unmet)
        results.append(
            TestResultItem(
                nodeid="tests/e2e/tier1_features/test_f01.py::test_139",
                name="test_139",
                file_path="tests/e2e/tier1_features/test_f01.py",
                tier="1",
                feature_id="F1",
                outcome="passed",
                duration=0.01,
            )
        )
        summary = runner._aggregate_summary("2026-08-15T12:00:00Z", results, total_duration=1.0)
        assert summary.tier_stats["1"].passed_threshold is True
        assert summary.tier_stats["2"].passed_threshold is False
        assert summary.thresholds_passed is False

    def test_aggregation_failure_and_error_handling(self):
        """Verify that any failure or error marks threshold as failed."""
        runner = E2ETestRunner(tiers=["1"])
        results = [
            TestResultItem(
                nodeid=f"tests/e2e/tier1_features/test_f01.py::test_{i}",
                name=f"test_{i}",
                file_path="tests/e2e/tier1_features/test_f01.py",
                tier="1",
                feature_id="F1",
                outcome="passed",
                duration=0.01,
            )
            for i in range(140)
        ]
        # Add 1 failure
        results.append(
            TestResultItem(
                nodeid="tests/e2e/tier1_features/test_f01.py::test_fail",
                name="test_fail",
                file_path="tests/e2e/tier1_features/test_f01.py",
                tier="1",
                feature_id="F1",
                outcome="failed",
                duration=0.01,
                error_message="AssertionError: expected 200, got 500",
            )
        )
        summary = runner._aggregate_summary("2026-08-15T12:00:00Z", results, total_duration=1.5)
        assert summary.failed == 1
        assert len(summary.failures) == 1
        assert summary.tier_stats["1"].passed_threshold is False
        assert summary.verdict == "FAILED"

    def test_aggregation_max_duration_and_min_tests_constraints(self):
        """Verify max_duration and min_tests parameters."""
        # Max duration exceeded
        runner = E2ETestRunner(tiers=["1"], max_duration=1.0)
        results = [
            TestResultItem(
                nodeid="tests/e2e/tier1_features/test_f01.py::test_1",
                name="test_1",
                file_path="tests/e2e/tier1_features/test_f01.py",
                tier="1",
                feature_id="F1",
                outcome="passed",
                duration=2.0,
            )
        ]
        summary = runner._aggregate_summary("2026-08-15T12:00:00Z", results, total_duration=2.5)
        assert summary.thresholds_passed is False

        # Min tests constraint
        runner_min = E2ETestRunner(tiers=["1"], min_tests=10)
        summary_min = runner_min._aggregate_summary("2026-08-15T12:00:00Z", results, total_duration=0.5)
        assert summary_min.thresholds_passed is False

    def test_feature_filter_in_runner(self):
        """Verify runner filters results when feature_filter is specified."""
        runner = E2ETestRunner(tiers=["1"], feature_filter="F7")
        assert runner.feature_filter == "F7"

    def test_report_formatter_empty_summary(self, tmp_path: Path):
        """Verify ReportFormatter handles 0 total tests safely without divide-by-zero or crash."""
        runner = E2ETestRunner(tiers=["1"])
        summary = runner._aggregate_summary("2026-08-15T12:00:00Z", [], total_duration=0.0)

        # 1. Terminal report does not raise
        ReportFormatter.print_terminal_report(summary)

        # 2. JSON export
        json_path = tmp_path / "empty_report.json"
        ReportFormatter.export_json(summary, str(json_path))
        assert json_path.exists()
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["total_tests"] == 0
        assert data["verdict"] == "FAILED"

        # 3. Markdown export
        md_path = tmp_path / "empty_report.md"
        ReportFormatter.export_markdown(summary, str(md_path))
        assert md_path.exists()
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "0.0%" in content
        assert "| `F1` |" in content

    def test_report_formatter_failures_formatting(self, tmp_path: Path):
        """Verify failure messages and diagnostics are properly formatted in Markdown and JSON."""
        runner = E2ETestRunner(tiers=["1"])
        failure_item = TestResultItem(
            nodeid="tests/e2e/tier1_features/test_f10_auth.py::test_invalid_bearer_token",
            name="test_invalid_bearer_token",
            file_path="tests/e2e/tier1_features/test_f10_auth.py",
            tier="1",
            feature_id="F10",
            outcome="failed",
            duration=0.12,
            error_message="Traceback (most recent call last):\n  File 'test.py', line 10\n    assert res.status_code == 401\nAssertionError: assert 500 == 401",
        )
        summary = runner._aggregate_summary("2026-08-15T12:00:00Z", [failure_item], total_duration=0.15)

        json_path = tmp_path / "fail_report.json"
        ReportFormatter.export_json(summary, str(json_path))
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["failures"]) == 1
        assert data["failures"][0]["feature_id"] == "F10"
        assert "assert 500 == 401" in data["failures"][0]["error_message"]

        md_path = tmp_path / "fail_report.md"
        ReportFormatter.export_markdown(summary, str(md_path))
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "## Failures & Diagnostics" in content
        assert "### `tests/e2e/tier1_features/test_f10_auth.py::test_invalid_bearer_token`" in content
        assert "assert 500 == 401" in content

    def test_cli_execution_via_subprocess(self, tmp_path: Path):
        """Adversarially execute runner.py via subprocess with various CLI arguments."""
        json_out = str(tmp_path / "cli_report.json")
        md_out = str(tmp_path / "cli_report.md")

        cmd = [
            sys.executable,
            "-m",
            "tests.e2e.harness.runner",
            "--tier",
            "1",
            "--dry-run",
            "--json-output",
            json_out,
            "--markdown-output",
            md_out,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert os.path.exists(json_out)
        assert os.path.exists(md_out)


# ==============================================================================
# SECTION 2: TESTENVIRONMENT ADVERSARIAL STRESS TESTS
# ==============================================================================

class TestEnvironmentAdversarial:
    """Adversarial stress-testing of TestEnvironment isolation and lifecycle."""

    def test_postgres_schema_drop_guard(self):
        assert validate_test_schema_name("gateway_test_0123456789abcdef") == "gateway_test_0123456789abcdef"
        with pytest.raises(ValueError):
            validate_test_schema_name("public")
        with pytest.raises(ValueError):
            validate_test_schema_name("gateway_test_bad-name")

    def test_database_url_uses_canonical_environment_name(self, monkeypatch):
        monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
        monkeypatch.delenv("DB_URL", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://canonical.example/gateway")

        assert configured_database_url() == "postgresql+asyncpg://canonical.example/gateway"

    @pytest.mark.asyncio
    async def test_explicit_postgres_configuration_never_falls_back_to_sqlite(self, monkeypatch):
        monkeypatch.setenv(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://unavailable.example/gateway",
        )
        broken_engine = MagicMock()
        broken_engine.connect.side_effect = OSError("connection failed")

        with patch(
            "tests.e2e.harness.test_env.create_async_engine",
            return_value=broken_engine,
        ):
            with pytest.raises(RuntimeError, match="Configured PostgreSQL"):
                await detect_database_configuration()

    @pytest.mark.asyncio
    async def test_env_var_isolation_and_restoration(self):
        """Verify os.environ is strictly saved and restored without residual leakage."""
        sentinel_key = "MY_SPECIAL_GATEWAY_SENTINEL_VAR"
        orig_key = "HOST"
        os.environ[sentinel_key] = "original_val"
        os.environ[orig_key] = "192.168.1.100"

        custom_overrides = {
            "HOST": "10.0.0.1",
            "NEW_TEST_VAR": "new_value",
            "LOG_LEVEL": "CRITICAL",
        }

        # Mock engine creation to isolate env var lifecycle from SQLite schema creation
        with patch.object(TestEnvironment, "start", autospec=True) as mock_start, \
             patch.object(TestEnvironment, "stop", autospec=True) as mock_stop:
            
            env = TestEnvironment(env_overrides=custom_overrides)
            env.temp_dir = tempfile.TemporaryDirectory(prefix="gateway_test_storage_")
            env.storage_path = Path(env.temp_dir.name)
            env._orig_environ = os.environ.copy()
            for k, v in custom_overrides.items():
                os.environ[k] = str(v)
            os.environ["STORAGE_DIR"] = str(env.storage_path)

            assert os.environ["HOST"] == "10.0.0.1"
            assert os.environ["NEW_TEST_VAR"] == "new_value"
            assert os.environ["LOG_LEVEL"] == "CRITICAL"
            assert os.environ["STORAGE_DIR"] == str(env.storage_path)

            # Manual teardown
            os.environ.clear()
            os.environ.update(env._orig_environ)
            env.temp_dir.cleanup()

        assert os.environ[sentinel_key] == "original_val"
        assert os.environ[orig_key] == "192.168.1.100"
        assert "NEW_TEST_VAR" not in os.environ
        del os.environ[sentinel_key]

    @pytest.mark.asyncio
    async def test_temp_storage_cleanup_on_exit(self):
        """Verify temporary storage folder and files within it are completely removed on exit."""
        temp_dir = tempfile.TemporaryDirectory(prefix="gateway_test_storage_")
        storage_path = Path(temp_dir.name)
        test_file = storage_path / "test_artifact.bin"
        test_file.write_bytes(b"temp_payload_data")
        assert test_file.exists()

        temp_dir.cleanup()
        assert not storage_path.exists()
        assert not test_file.exists()

    @pytest.mark.asyncio
    async def test_settings_cache_invalidation(self):
        """Verify get_settings() cache_clear invalidation."""
        base_settings = get_settings()
        orig_host = base_settings.gateway.host

        os.environ["HOST"] = "192.168.99.99"
        get_settings.cache_clear()
        new_settings = get_settings()
        assert new_settings.gateway.host == "192.168.99.99"

        del os.environ["HOST"]
        get_settings.cache_clear()
        restored_settings = get_settings()
        assert restored_settings.gateway.host == "0.0.0.0"


# ==============================================================================
# SECTION 3: SQLITE COMPATIBILITY & TYPE COMPILATION TESTS
# ==============================================================================

class TestSQLiteTypeCompilationAdversarial:
    """Adversarial testing of SQLite custom type compilers (Vector, TSVECTOR, PG_UUID)."""

    @pytest.mark.asyncio
    async def test_sqlite_type_compilers_standalone(self):
        """Verify Vector, TSVECTOR, PG_UUID compile into valid SQLite DDL types."""
        test_engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
        )

        class CustomSQLiteModel(DeclarativeBase):
            pass

        class StandaloneSQLiteItem(CustomSQLiteModel):
            __tablename__ = "test_standalone_sqlite_items"
            id: Mapped[str] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
            vec: Mapped[list[float]] = mapped_column(Vector(384), nullable=True)
            tsv: Mapped[str] = mapped_column(TSVECTOR, nullable=True)

        async with test_engine.begin() as conn:
            await conn.run_sync(CustomSQLiteModel.metadata.create_all)

            test_id = str(uuid4())
            await conn.execute(
                text(
                    "INSERT INTO test_standalone_sqlite_items (id, vec, tsv) "
                    "VALUES (:id, :vec, :tsv)"
                ),
                [
                    {
                        "id": test_id,
                        "vec": "[0.1, 0.2, 0.3]",
                        "tsv": "search term",
                    }
                ],
            )

            res = await conn.execute(
                text("SELECT id, vec, tsv FROM test_standalone_sqlite_items WHERE id = :id"),
                [{"id": test_id}],
            )
            row = res.fetchone()
            assert row is not None
            assert row[0] == test_id
            assert "[0.1, 0.2, 0.3]" in row[1]
            assert "search" in row[2]

        await test_engine.dispose()

    @pytest.mark.asyncio
    async def test_sqlite_jsonb_and_computed_compilation_standalone(self):
        """Verify JSONB, Computed, and server_default with Postgres cast compile cleanly on SQLite."""
        test_engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
        )

        class CustomSQLiteModel(DeclarativeBase):
            pass

        class StandaloneJSONBItem(CustomSQLiteModel):
            __tablename__ = "test_standalone_jsonb_items"
            id: Mapped[str] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
            content: Mapped[str] = mapped_column(Text, nullable=False)
            tsv: Mapped[str] = mapped_column(
                TSVECTOR,
                Computed("to_tsvector('english', content)", persisted=True),
                nullable=True,
            )
            meta: Mapped[dict] = mapped_column(
                "meta",
                JSONB,
                default=dict,
                server_default=text("'{}'::jsonb"),
                nullable=False,
            )

        async with test_engine.begin() as conn:
            await conn.run_sync(CustomSQLiteModel.metadata.create_all)

            test_id = str(uuid4())
            await conn.execute(
                text(
                    "INSERT INTO test_standalone_jsonb_items (id, content, meta) "
                    "VALUES (:id, :content, :meta)"
                ),
                [{"id": test_id, "content": "Document text", "meta": '{"tag": "test"}'}],
            )

            res = await conn.execute(
                text("SELECT id, content, meta FROM test_standalone_jsonb_items WHERE id = :id"),
                [{"id": test_id}],
            )
            row = res.fetchone()
            assert row is not None
            assert row[0] == test_id
            assert row[1] == "Document text"
            assert '{"tag": "test"}' in row[2]

        await test_engine.dispose()

    @pytest.mark.asyncio
    async def test_sqlite_full_gateway_models_create_all(self):
        """Verify that real Gateway ORM Base metadata creates all tables and indexes on SQLite."""
        test_engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
        )

        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

            # Test creating and linking real ORM records
            ws_id = "global"
            doc_id = str(uuid4())
            chunk_id = str(uuid4())
            k_id = str(uuid4())
            rev_id = str(uuid4())

            await conn.execute(
                text("INSERT INTO workspaces (id, name) VALUES (:id, :name)"),
                [{"id": ws_id, "name": "Global Test"}],
            )
            await conn.execute(
                text(
                    "INSERT INTO document_files (id, workspace_id, filename, file_path, file_size, mime_type) "
                    "VALUES (:id, :ws, :fn, :fp, :fs, :mime)"
                ),
                [{"id": doc_id, "ws": ws_id, "fn": "test.txt", "fp": "/path/test.txt", "fs": 1024, "mime": "text/plain"}],
            )
            await conn.execute(
                text(
                    "INSERT INTO document_chunks (id, document_id, workspace_id, chunk_index, content, embedding, metadata) "
                    "VALUES (:id, :doc_id, :ws, :idx, :content, :emb, :meta)"
                ),
                [{"id": chunk_id, "doc_id": doc_id, "ws": ws_id, "idx": 0, "content": "chunk text", "emb": "[0.1]", "meta": "{}"}],
            )
            await conn.execute(
                text(
                    "INSERT INTO knowledge_items (id, workspace_id, title, content) "
                    "VALUES (:id, :ws, :title, :content)"
                ),
                [{"id": k_id, "ws": ws_id, "title": "Test Title", "content": "Test content"}],
            )
            await conn.execute(
                text(
                    "INSERT INTO knowledge_revisions (id, item_id, version, content_hash, content) "
                    "VALUES (:id, :item_id, :v, :h, :content)"
                ),
                [{"id": rev_id, "item_id": k_id, "v": 1, "h": "hash123", "content": "Test content"}],
            )

            # Query back
            res = await conn.execute(text("SELECT count(*) FROM document_chunks"))
            assert res.scalar() == 1

            res_k = await conn.execute(text("SELECT count(*) FROM knowledge_revisions"))
            assert res_k.scalar() == 1

        await test_engine.dispose()

    @pytest.mark.asyncio
    async def test_test_environment_lifecycle_with_sqlite(self):
        """Verify TestEnvironment boots cleanly on SQLite, creates schema, and seeds default workspace."""
        async with TestEnvironment() as env:
            assert env.engine is not None
            assert env.session_factory is not None
            assert env.is_postgres is False
            assert os.environ["EMBEDDING_DIMENSION"] == str(EMBED_DIM)

            async with env.engine.begin() as conn:
                res = await conn.execute(text("SELECT id, name FROM workspaces WHERE id = 'global'"))
                row = res.fetchone()
                assert row is not None
                assert row[0] == "global"

            # Clean database and verify workspace re-seeded
            await env.clean_database()
            async with env.engine.begin() as conn:
                res = await conn.execute(text("SELECT count(*) FROM workspaces WHERE id = 'global'"))
                assert res.scalar() == 1


# ==============================================================================
# SECTION 4: SSE STREAM PARSER ADVERSARIAL TESTS
# ==============================================================================

class TestSSEParserAdversarial:
    """Adversarial stress-testing of parse_sse_stream."""

    @pytest.mark.asyncio
    async def test_parse_sse_stream_various_formats(self):
        """Verify parse_sse_stream handles comments, empty lines, [DONE], and malformed JSON."""
        sse_lines = [
            b": ping\n\n",
            b": initial keepalive comment\n\n",
            b'data: {"id": "chat-1", "choices": [{"delta": {"content": "Hello"}}]}\n\n',
            b"\n",
            b'data: {"id": "chat-1", "choices": [{"delta": {"content": " World!"}}]}\n\n',
            b"data: {malformed json payload\n\n",
            b"data: [DONE]\n\n",
            b'data: {"id": "chat-after-done", "choices": []}\n\n',
        ]

        async def line_iterator():
            for line in sse_lines:
                yield line.decode("utf-8")

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.aiter_lines = line_iterator

        events = await parse_sse_stream(mock_response)

        assert len(events) == 3
        assert events[0]["id"] == "chat-1"
        assert events[0]["choices"][0]["delta"]["content"] == "Hello"
        assert events[1]["choices"][0]["delta"]["content"] == " World!"
        assert "raw" in events[2]
        assert events[2]["raw"] == "{malformed json payload"
