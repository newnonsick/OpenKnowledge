"""
Unit / Self-tests for Standalone E2E Test Runner & Pytest Plugin.

Tests:
1. Feature ID extraction & regex matching
2. PytestResultCollector logreport interception
3. SuiteSummary & TierStatistics aggregation
4. ReportFormatter export to JSON & Markdown
5. E2ETestRunner programmatic execution
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from tests.e2e.harness.runner import (
    FEATURES,
    TIER_DIRS,
    TIER_THRESHOLDS,
    E2ETestRunner,
    PytestResultCollector,
    ReportFormatter,
    SuiteSummary,
    TestResultItem,
    TierStatistics,
)


def test_feature_registry_completeness():
    """Verify all 28 features (F1 to F28) are registered with non-empty descriptions."""
    assert len(FEATURES) == 28
    for i in range(1, 29):
        assert i in FEATURES
        code, name = FEATURES[i]
        assert code == f"F{i}"
        assert len(name) > 0


def test_feature_id_extraction():
    """Verify regex extraction of feature codes from various path conventions."""
    collector = PytestResultCollector()

    assert collector._extract_feature_id("test_f01_settings.py::test_case", "tests/e2e/tier1_features/test_f01_settings.py") == "F1"
    assert collector._extract_feature_id("test_f7_openai.py::test_case", "tests/e2e/tier1_features/test_f7_openai.py") == "F7"
    assert collector._extract_feature_id("test_f24_orchestration_bounds.py::test_case", "tests/e2e/tier2_boundaries/test_f24_orchestration_bounds.py") == "F24"
    assert collector._extract_feature_id("test_feature_28_suite.py::test_case", "tests/e2e/tier1_features/test_feature_28_suite.py") == "F28"
    assert collector._extract_feature_id("test_other.py::test_case", "tests/e2e/tier4_scenarios/test_s01_onboarding.py") is None


def test_suite_summary_aggregation():
    """Verify aggregation of test results into TierStatistics and Feature Matrix."""
    runner = E2ETestRunner(tiers=["1", "2"])

    sample_results = [
        TestResultItem(
            nodeid="tests/e2e/tier1_features/test_f01_settings.py::test_1",
            name="test_1",
            file_path="tests/e2e/tier1_features/test_f01_settings.py",
            tier="1",
            feature_id="F1",
            outcome="passed",
            duration=0.05,
        ),
        TestResultItem(
            nodeid="tests/e2e/tier1_features/test_f01_settings.py::test_2",
            name="test_2",
            file_path="tests/e2e/tier1_features/test_f01_settings.py",
            tier="1",
            feature_id="F1",
            outcome="passed",
            duration=0.05,
        ),
        TestResultItem(
            nodeid="tests/e2e/tier2_boundaries/test_f07_openai_bounds.py::test_1",
            name="test_1",
            file_path="tests/e2e/tier2_boundaries/test_f07_openai_bounds.py",
            tier="2",
            feature_id="F7",
            outcome="passed",
            duration=0.10,
        ),
    ]

    summary = runner._aggregate_summary("2026-08-15T12:00:00Z", sample_results, total_duration=0.20)

    assert summary.total_tests == 3
    assert summary.passed == 3
    assert summary.failed == 0
    assert summary.tier_stats["1"].total == 2
    assert summary.tier_stats["1"].passed == 2
    assert summary.tier_stats["2"].total == 1
    assert summary.tier_stats["2"].passed == 1
    assert summary.feature_matrix["F1"]["1"] == 2
    assert summary.feature_matrix["F7"]["2"] == 1


def test_report_export_json_and_markdown(tmp_path: Path):
    """Verify ReportFormatter exports valid JSON and Markdown files."""
    runner = E2ETestRunner(tiers=["1"])
    sample_results = [
        TestResultItem(
            nodeid="tests/e2e/tier1_features/test_f01.py::test_case",
            name="test_case",
            file_path="tests/e2e/tier1_features/test_f01.py",
            tier="1",
            feature_id="F1",
            outcome="passed",
            duration=0.02,
        )
    ]
    summary = runner._aggregate_summary("2026-08-15T12:00:00Z", sample_results, total_duration=0.05)

    # 1. Terminal report does not raise
    ReportFormatter.print_terminal_report(summary)

    # 2. JSON export
    json_path = str(tmp_path / "summary.json")
    ReportFormatter.export_json(summary, json_path)
    assert Path(json_path).exists()
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["total_tests"] == 1
    assert data["passed"] == 1

    # 3. Markdown export
    md_path = str(tmp_path / "summary.md")
    ReportFormatter.export_markdown(summary, md_path)
    assert Path(md_path).exists()
    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "# E2E Test Execution Summary Report" in content
    assert "F1" in content


def test_programmatic_runner_execution():
    """Verify E2ETestRunner can run pytest programmatically on unit tests."""
    runner = E2ETestRunner(
        tiers=["1"],
        custom_paths=["tests/unit/test_mock_server.py"],
        verbose=True,
    )
    summary = runner.run()
    assert summary.total_tests > 0
    assert summary.failed == 0
    assert summary.passed == summary.total_tests
