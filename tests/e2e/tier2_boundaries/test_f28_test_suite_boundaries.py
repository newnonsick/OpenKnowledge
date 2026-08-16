"""Tier 2 Boundary Tests for Feature 28: Comprehensive Automated Test Suite.

Tests boundary conditions, test runner execution, non-existent tiers, threshold validations, and report exporters.
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


@pytest.mark.tier2
@pytest.mark.feature("F28")
def test_f28_boundary_runner_non_existent_tier_execution():
    """Test boundary: runner handling unknown tier IDs ('99', 'invalid_tier')."""
    runner = E2ETestRunner(tiers=["99", "invalid"], dry_run=True)
    summary = runner.run()
    assert summary.total_tests == 0
    assert summary.verdict in ("PASSED", "FAILED")


@pytest.mark.tier2
@pytest.mark.feature("F28")
def test_f28_boundary_runner_feature_filter_unmatched_returns_empty():
    """Test boundary: filtering runner by non-existent feature ('F999') yields 0 collected tests."""
    runner = E2ETestRunner(tiers=["2"], feature_filter="F999", dry_run=True)
    summary = runner.run()
    assert summary.total_tests == 0


@pytest.mark.tier2
@pytest.mark.feature("F28")
def test_f28_boundary_runner_max_duration_threshold_violation():
    """Test boundary: summary marks verdict as FAILED when total duration exceeds max_duration."""
    runner = E2ETestRunner(tiers=["2"], max_duration=0.000001)
    # Simulate aggregation with long duration
    fake_results = [
        TestResultItem(
            nodeid="test1", name="test1", file_path="f.py", tier="2", feature_id="F28",
            outcome="passed", duration=5.0
        )
    ]
    summary = runner._aggregate_summary("2026-08-15T00:00:00Z", fake_results, total_duration=5.0)
    assert summary.thresholds_passed is False
    assert summary.verdict == "FAILED"


@pytest.mark.tier2
@pytest.mark.feature("F28")
def test_f28_boundary_pytest_result_collector_extract_feature_id_variations():
    """Test boundary: PytestResultCollector extracts feature IDs across varied naming conventions."""
    collector = PytestResultCollector()

    assert collector._extract_feature_id("tests/test_f01.py::test_a", "tests/test_f01.py") == "F1"
    assert collector._extract_feature_id("tests/test_f28.py::test_b", "tests/test_f28.py") == "F28"
    assert collector._extract_feature_id("tests/test_f07_chat.py::test_c", "tests/test_f07_chat.py") == "F7"
    assert collector._extract_feature_id("tests/test_unknown.py::test_d", "tests/test_unknown.py") is None


@pytest.mark.tier2
@pytest.mark.feature("F28")
def test_f28_boundary_json_and_markdown_report_exporters():
    """Test boundary: ReportFormatter JSON and Markdown export functions with empty failures list."""
    summary = SuiteSummary(
        timestamp="2026-08-15T12:00:00Z",
        total_tests=10,
        passed=10,
        failed=0,
        skipped=0,
        errors=0,
        total_duration=1.23,
        verdict="PASSED",
        thresholds_passed=True,
    )
    summary.tier_stats["2"] = TierStatistics(tier="2", name="Tier 2", total=10, passed=10, passed_threshold=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = str(Path(tmpdir) / "sub" / "report.json")
        md_path = str(Path(tmpdir) / "sub" / "report.md")

        ReportFormatter.export_json(summary, json_path)
        assert Path(json_path).exists()

        ReportFormatter.export_markdown(summary, md_path)
        assert Path(md_path).exists()
        md_content = Path(md_path).read_text(encoding="utf-8")
        assert "PASSED" in md_content
