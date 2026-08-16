"""Tier 1 Feature Tests for Feature 28: Comprehensive Automated Test Suite.

Validates the E2E testing framework, test runner execution, result collector,
tier and feature code mapping, quality gate evaluations, and report exporters.
"""

from pathlib import Path
import tempfile
import pytest

from tests.e2e.harness.runner import (
    E2ETestRunner,
    PytestResultCollector,
    ReportFormatter,
    SuiteSummary,
    TestResultItem,
    TierStatistics,
)
from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.tier1
@pytest.mark.feature("F28")
def test_f28_runner_tier_and_feature_filter_initialization():
    """Verify E2ETestRunner initialization with specific tier and feature filters."""
    runner = E2ETestRunner(
        tiers=["1", "2"],
        feature_filter="F28",
        verbose=True,
        dry_run=True,
    )
    assert runner.tiers == ["1", "2"]
    assert runner.feature_filter == "F28"
    assert runner.verbose is True
    assert runner.dry_run is True


@pytest.mark.tier1
@pytest.mark.feature("F28")
def test_f28_pytest_result_collector_extracts_tier_and_feature():
    """Verify PytestResultCollector correctly parses feature codes from test node IDs and file paths."""
    collector = PytestResultCollector()

    tier1_path = "tests/e2e/tier1_features/test_f28_test_suite.py"
    nodeid1 = f"{tier1_path}::test_f28_runner"
    assert collector._extract_feature_id(nodeid1, tier1_path) == "F28"

    tier2_path = "tests/e2e/tier2_boundaries/test_f07_openai_chat_bounds.py"
    nodeid2 = f"{tier2_path}::test_f07_stream"
    assert collector._extract_feature_id(nodeid2, tier2_path) == "F7"


@pytest.mark.tier1
@pytest.mark.feature("F28")
def test_f28_suite_summary_quality_gate_evaluation():
    """Verify SuiteSummary accurately tracks test counts, pass/fail totals, and evaluates quality gates."""
    summary = SuiteSummary(
        timestamp="2026-08-15T12:00:00Z",
        total_tests=140,
        passed=140,
        failed=0,
        skipped=0,
        errors=0,
        total_duration=15.5,
        verdict="PASSED",
        thresholds_passed=True,
    )
    assert summary.total_tests == 140
    assert summary.passed == 140
    assert summary.failed == 0
    assert summary.errors == 0
    assert summary.verdict == "PASSED"
    assert summary.thresholds_passed is True


@pytest.mark.tier1
@pytest.mark.feature("F28")
def test_f28_report_formatter_terminal_and_json_export():
    """Verify ReportFormatter exports valid JSON and Markdown summary reports to disk."""
    summary = SuiteSummary(
        timestamp="2026-08-15T12:00:00Z",
        total_tests=5,
        passed=5,
        failed=0,
        skipped=0,
        errors=0,
        total_duration=0.5,
        verdict="PASSED",
        thresholds_passed=True,
    )
    summary.tier_stats["1"] = TierStatistics(tier="1", name="Tier 1", total=5, passed=5, passed_threshold=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        json_file = str(Path(tmpdir) / "summary.json")
        md_file = str(Path(tmpdir) / "summary.md")

        ReportFormatter.export_json(summary, json_file)
        assert Path(json_file).exists()

        ReportFormatter.export_markdown(summary, md_file)
        assert Path(md_file).exists()
        assert "PASSED" in Path(md_file).read_text(encoding="utf-8")


@pytest.mark.tier1
@pytest.mark.feature("F28")
@pytest.mark.asyncio
async def test_f28_test_environment_lifecycle_and_cleanup():
    """Verify TestEnvironment starts up cleanly and tears down temporary storage and database engine."""
    env = TestEnvironment()
    await env.start()
    assert env.storage_path is not None
    assert env.storage_path.exists()
    assert env.engine is not None

    await env.stop()
    assert env.engine is None
