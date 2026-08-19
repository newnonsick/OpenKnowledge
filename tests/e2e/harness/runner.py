"""
Standalone E2E Test Runner for Local AI Gateway.

Executes tests tier-by-tier or in aggregate, maps test cases against all 29 features,
validates pass/fail thresholds, and produces structured terminal, JSON, and Markdown reports.

Usage:
    python -m tests.e2e.harness.runner --tier all
    python -m tests.e2e.harness.runner --tier 1 --feature F7
    python -m tests.e2e.harness.runner --tier 2 --json-output reports/t2_summary.json
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pytest

# ==============================================================================
# ==============================================================================

FEATURES: Dict[int, Tuple[str, str]] = {
    1: ("F1", "Decoupled Pydantic Settings & .env.example"),
    2: ("F2", "Pragmatic Clean Architecture Core"),
    3: ("F3", "SQLAlchemy 2.0 Async + pgvector Models"),
    4: ("F4", "Explicit Alembic Migration Release Flow"),
    5: ("F5", "Global Workspace Bootstrapping"),
    6: ("F6", "Local Disk Storage Adapter"),
    7: ("F7", "OpenAI /v1/chat/completions API"),
    8: ("F8", "Anthropic /v1/messages API"),
    9: ("F9", "Model Registry & Alias Mapping"),
    10: ("F10", "API Key Authentication Middleware"),
    11: ("F11", "Health & Discovery Endpoints"),
    12: ("F12", "Knowledge Domain Model & SHA-256 Hashing"),
    13: ("F13", "Optimistic Concurrency Control (OCC)"),
    14: ("F14", "Soft Deletion & Workspace Scoping"),
    15: ("F15", "Knowledge Tool Schemas"),
    16: ("F16", "File Upload Endpoint (POST /v1/files/upload)"),
    17: ("F17", "Multi-Format Document Parsers"),
    18: ("F18", "Fixed & Semantic Chunking"),
    19: ("F19", "HTTP Embedding Client & Vector Indexing"),
    20: ("F20", "Full-Text Search Contract"),
    21: ("F21", "Semantic Search Contract"),
    22: ("F22", "Reciprocal Rank Fusion (RRF) & Deduplication"),
    23: ("F23", "Structured Context Attribution"),
    24: ("F24", "Chat Orchestration & Tool Interception Loop"),
    25: ("F25", "External Harness Tool Passthrough"),
    26: ("F26", "Streaming SSE Tool Interception Engine"),
    27: ("F27", "Max Tool Iteration Guardrail"),
    28: ("F28", "Comprehensive Automated Test Suite"),
    29: ("F29", "Thinking / Reasoning Pass-Through"),
}

TIER_DIRS: Dict[str, str] = {
    "1": "tests/e2e/tier1_features",
    "2": "tests/e2e/tier2_boundaries",
    "3": "tests/e2e/tier3_combinations",
    "4": "tests/e2e/tier4_scenarios",
    "5": "tests/e2e/tier5_adversarial",
}

TIER_NAMES: Dict[str, str] = {
    "1": "Tier 1: Feature Coverage (>=5 per feature)",
    "2": "Tier 2: Boundary & Corner Cases (>=5 per feature)",
    "3": "Tier 3: Combinations & Pairwise (>=28 tests)",
    "4": "Tier 4: Real-World Scenarios (>=14 tests)",
    "5": "Tier 5: Adversarial Coverage Hardening",
}

TIER_THRESHOLDS: Dict[str, int] = {
    "1": 140,
    "2": 140,
    "3": 28,
    "4": 14,
    "5": 20,
}


# ==============================================================================
# Data Models for Results & Reports
# ==============================================================================

@dataclass
class TestResultItem:
    __test__ = False
    nodeid: str
    name: str
    file_path: str
    tier: str
    feature_id: Optional[str]
    outcome: str  # "passed", "failed", "skipped", "error"
    duration: float
    error_message: Optional[str] = None
    stdout: Optional[str] = None


@dataclass
class TierStatistics:
    tier: str
    name: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    duration: float = 0.0
    threshold_min: int = 0
    passed_threshold: bool = False


@dataclass
class SuiteSummary:
    timestamp: str
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    total_duration: float = 0.0
    tier_stats: Dict[str, TierStatistics] = field(default_factory=dict)
    feature_matrix: Dict[str, Dict[str, int]] = field(default_factory=dict)
    failures: List[TestResultItem] = field(default_factory=list)
    verdict: str = "PENDING"
    thresholds_passed: bool = False


# ==============================================================================
# Custom Pytest Plugin for Direct Result Interception
# ==============================================================================

class PytestResultCollector:
    """Pytest plugin hook that captures detailed test execution data."""

    def __init__(self, target_tier: Optional[str] = None):
        self.target_tier = target_tier
        self.results: List[TestResultItem] = []
        self.collected_results: List[TestResultItem] = []
        self.unknown_feature_ids: Set[str] = set()

    def pytest_collection_finish(self, session):
        for item in session.items:
            nodeid = item.nodeid
            file_path = nodeid.split("::")[0]
            marker_feature_id = self._extract_marker_feature_id(item)
            raw_feature_id = marker_feature_id or self._extract_raw_feature_id(nodeid, file_path)
            if raw_feature_id and raw_feature_id not in {value[0] for value in FEATURES.values()}:
                self.unknown_feature_ids.add(raw_feature_id)
            tier = "unknown"
            normalized_file_path = file_path.replace("\\", "/")
            for tier_id, dir_path in TIER_DIRS.items():
                if dir_path in normalized_file_path:
                    tier = tier_id
                    break
            self.collected_results.append(
                TestResultItem(
                    nodeid=nodeid,
                    name=nodeid.split("::")[-1],
                    file_path=file_path,
                    tier=tier,
                    feature_id=(
                        raw_feature_id
                        if raw_feature_id in {value[0] for value in FEATURES.values()}
                        else None
                    ),
                    outcome="collected",
                    duration=0.0,
                )
            )

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or (report.when in ("setup", "teardown") and report.failed):
            file_path = report.nodeid.split("::")[0]
            name = report.nodeid.split("::")[-1]

            tier = "unknown"
            normalized_file_path = file_path.replace("\\", "/")
            for t, dir_path in TIER_DIRS.items():
                if dir_path in normalized_file_path:
                    tier = t
                    break

            feature_id = self._extract_feature_id(report.nodeid, file_path)

            outcome = report.outcome
            if report.failed:
                outcome = "failed"
            elif report.skipped:
                outcome = "skipped"

            error_msg = None
            if report.longrepr:
                error_msg = str(report.longrepr)

            item = TestResultItem(
                nodeid=report.nodeid,
                name=name,
                file_path=file_path,
                tier=tier,
                feature_id=feature_id,
                outcome=outcome,
                duration=getattr(report, "duration", 0.0),
                error_message=error_msg,
            )
            self.results.append(item)

    def _extract_feature_id(self, nodeid: str, file_path: str) -> Optional[str]:
        feature_id = self._extract_raw_feature_id(nodeid, file_path)
        if feature_id in {value[0] for value in FEATURES.values()}:
            return feature_id
        return None

    def _extract_raw_feature_id(self, nodeid: str, file_path: str) -> Optional[str]:
        match = re.search(r"f(?:eature)?_?0?(\d{1,2})", file_path, re.IGNORECASE)
        if match:
            return f"F{int(match.group(1))}"
        match_node = re.search(r"f(?:eature)?_?0?(\d{1,2})", nodeid, re.IGNORECASE)
        if match_node:
            return f"F{int(match_node.group(1))}"
        return None

    def _extract_marker_feature_id(self, item: Any) -> Optional[str]:
        markers = list(item.iter_markers(name="feature"))
        if not markers:
            return None
        feature_ids = []
        for marker in markers:
            for value in marker.args:
                if not isinstance(value, str):
                    continue
                match = re.fullmatch(r"F0?(\d+)", value.strip(), re.IGNORECASE)
                if match:
                    feature_ids.append(f"F{int(match.group(1))}")
        unique_ids = set(feature_ids)
        if len(unique_ids) > 1:
            return "INVALID"
        return next(iter(unique_ids)) if unique_ids else None


# ==============================================================================
# Runner Engine
# ==============================================================================

class E2ETestRunner:
    def __init__(
        self,
        tiers: List[str],
        feature_filter: Optional[str] = None,
        keyword: Optional[str] = None,
        verbose: bool = False,
        fail_fast: bool = False,
        max_duration: Optional[float] = None,
        min_tests: Optional[int] = None,
        dry_run: bool = False,
        custom_paths: Optional[List[str]] = None,
    ):
        self.tiers = tiers
        self.feature_filter = feature_filter.upper() if feature_filter else None
        self.keyword = keyword
        self.verbose = verbose
        self.fail_fast = fail_fast
        self.max_duration = max_duration
        self.min_tests = min_tests
        self.dry_run = dry_run
        self.custom_paths = custom_paths

    def run(self) -> SuiteSummary:
        start_time = time.time()
        timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        collector = PytestResultCollector()

        unknown_tiers = [
            tier for tier in self.tiers if tier not in TIER_DIRS and tier != "all"
        ]
        if unknown_tiers:
            raise ValueError(f"Unknown test tiers: {', '.join(unknown_tiers)}")

        pytest_args = ["-q", "--tb=short"]
        if self.verbose:
            pytest_args.append("-v")
        if self.fail_fast:
            pytest_args.append("-x")
        if self.keyword:
            pytest_args.extend(["-k", self.keyword])
        if self.dry_run:
            pytest_args.append("--collect-only")

        if self.custom_paths:
            test_paths = [p for p in self.custom_paths if Path(p).exists()]
        else:
            test_paths = []
            for tier in self.tiers:
                if tier in TIER_DIRS:
                    tier_dir = Path(TIER_DIRS[tier])
                    if tier_dir.exists():
                        test_paths.append(str(tier_dir))

        registered_features = {value[0] for value in FEATURES.values()}
        for test_path in test_paths:
            feature_id = collector._extract_raw_feature_id(test_path, test_path)
            if feature_id and feature_id not in registered_features:
                collector.unknown_feature_ids.add(feature_id)

        if collector.unknown_feature_ids:
            unknown = ", ".join(sorted(collector.unknown_feature_ids))
            raise ValueError(f"Unregistered feature identifiers collected: {unknown}")

        print(f"\n[E2E-RUNNER] Launching E2E Test Suite [Tiers: {', '.join(self.tiers)}]")
        print(f"Target paths: {test_paths}\n")

        pytest_exit_code = pytest.ExitCode.NO_TESTS_COLLECTED
        if test_paths:
            pytest_args.extend(test_paths)
            pytest_exit_code = pytest.main(pytest_args, plugins=[collector])

        if collector.unknown_feature_ids:
            unknown = ", ".join(sorted(collector.unknown_feature_ids))
            raise ValueError(f"Unregistered feature identifiers collected: {unknown}")

        if pytest_exit_code not in (pytest.ExitCode.OK, pytest.ExitCode.TESTS_FAILED):
            raise RuntimeError(
                f"pytest collection failed with exit code {int(pytest_exit_code)}"
            )

        elapsed_total = time.time() - start_time

        collected_results = collector.collected_results if self.dry_run else collector.results
        if self.feature_filter:
            collected_results = [
                r for r in collected_results if r.feature_id == self.feature_filter
            ]
            if not collected_results:
                raise ValueError(f"No tests matched feature {self.feature_filter}")

        if self.dry_run and not collected_results:
            raise RuntimeError("pytest collection failed because no tests were collected")

        summary = self._aggregate_summary(
            timestamp,
            collected_results,
            elapsed_total,
            collection_only=self.dry_run,
        )
        return summary

    def _aggregate_summary(
        self,
        timestamp: str,
        results: List[TestResultItem],
        total_duration: float,
        collection_only: bool = False,
    ) -> SuiteSummary:
        summary = SuiteSummary(
            timestamp=timestamp,
            total_tests=len(results),
            total_duration=total_duration,
        )

        for tier in ("1", "2", "3", "4", "5"):
            if tier in self.tiers or "all" in self.tiers:
                summary.tier_stats[tier] = TierStatistics(
                    tier=tier,
                    name=TIER_NAMES.get(tier, f"Tier {tier}"),
                    threshold_min=TIER_THRESHOLDS.get(tier, 0),
                )

        for num, (f_code, f_name) in FEATURES.items():
            summary.feature_matrix[f_code] = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "total": 0}

        for item in results:
            if item.outcome == "passed":
                summary.passed += 1
            elif item.outcome == "failed":
                summary.failed += 1
                summary.failures.append(item)
            elif item.outcome == "skipped":
                summary.skipped += 1
            elif item.outcome != "collected":
                summary.errors += 1
                summary.failures.append(item)

            if item.tier in summary.tier_stats:
                ts = summary.tier_stats[item.tier]
                ts.total += 1
                ts.duration += item.duration
                if item.outcome == "passed":
                    ts.passed += 1
                elif item.outcome == "failed":
                    ts.failed += 1
                elif item.outcome == "skipped":
                    ts.skipped += 1
                elif item.outcome != "collected":
                    ts.errors += 1

            if item.feature_id and item.feature_id in summary.feature_matrix:
                if item.tier in ("1", "2", "3", "4", "5"):
                    summary.feature_matrix[item.feature_id][item.tier] += 1 if collection_only or item.outcome == "passed" else 0
                summary.feature_matrix[item.feature_id]["total"] += 1 if collection_only or item.outcome == "passed" else 0

        if collection_only:
            summary.thresholds_passed = True
            summary.verdict = "COLLECTED"
            return summary

        all_passed = summary.failed == 0 and summary.errors == 0 and summary.total_tests > 0
        thresholds_ok = True

        for tier, ts in summary.tier_stats.items():
            if ts.total >= ts.threshold_min and ts.failed == 0 and ts.errors == 0:
                ts.passed_threshold = True
            else:
                ts.passed_threshold = False
                if tier in self.tiers:
                    thresholds_ok = False

        if self.min_tests and summary.total_tests < self.min_tests:
            thresholds_ok = False

        if self.max_duration and summary.total_duration > self.max_duration:
            thresholds_ok = False

        summary.thresholds_passed = all_passed and thresholds_ok
        summary.verdict = "PASSED" if summary.thresholds_passed else "FAILED"
        return summary


# ==============================================================================
# Report Generators (Console, JSON, Markdown)
# ==============================================================================

class ReportFormatter:
    @staticmethod
    def print_terminal_report(summary: SuiteSummary):
        c_green = "\033[92m"
        c_red = "\033[91m"
        c_yellow = "\033[93m"
        c_cyan = "\033[96m"
        c_bold = "\033[1m"
        c_reset = "\033[0m"

        verdict_color = (
            c_green
            if summary.verdict == "PASSED"
            else c_cyan
            if summary.verdict == "COLLECTED"
            else c_red
        )

        print("\n" + "=" * 80)
        print(f"{c_bold}{c_cyan}LOCAL AI GATEWAY -- E2E TEST EXECUTION REPORT{c_reset}")
        print("=" * 80)
        print(f"Timestamp:      {summary.timestamp}")
        print(f"Total Tests:    {summary.total_tests}")
        print(f"Passed:         {c_green}{summary.passed}{c_reset}")
        print(f"Failed:         {c_red}{summary.failed}{c_reset}")
        print(f"Skipped:        {c_yellow}{summary.skipped}{c_reset}")
        print(f"Errors:         {c_red}{summary.errors}{c_reset}")
        print(f"Total Duration: {summary.total_duration:.2f}s")
        print(f"Final Verdict:  {c_bold}{verdict_color}{summary.verdict}{c_reset}")
        print("-" * 80)

        # Tier Breakdown Table
        print(f"\n{c_bold}=== TEST TIER BREAKDOWN ==={c_reset}")
        print(
            f"{'Tier':<8} | {'Total':<7} | {'Passed':<7} | {'Failed':<7} | {'Duration':<10} | {'Threshold':<10} | {'Status'}"
        )
        print("-" * 75)
        for tier, ts in sorted(summary.tier_stats.items()):
            if summary.verdict == "COLLECTED":
                status_str = f"{c_cyan}COLLECTED{c_reset}"
                threshold = "n/a"
            else:
                status_str = (
                    f"{c_green}MET{c_reset}"
                    if ts.passed_threshold
                    else f"{c_red}UNMET{c_reset}"
                )
                threshold = f">={ts.threshold_min}"
            print(
                f"Tier {tier:<3} | {ts.total:<7} | {ts.passed:<7} | {ts.failed:<7} | "
                f"{ts.duration:<9.2f}s | {threshold:<10} | {status_str}"
            )
        print("-" * 75)

        # Feature Coverage Matrix
        print(f"\n{c_bold}=== FEATURE COVERAGE MATRIX (29 Features) ==={c_reset}")
        print(
            f"{'ID':<5} | {'Feature Description':<45} | {'T1':<4} | {'T2':<4} | {'T3':<4} | {'T4':<4} | {'Total'}"
        )
        print("-" * 80)
        for num, (f_code, f_name) in FEATURES.items():
            counts = summary.feature_matrix.get(
                f_code, {"1": 0, "2": 0, "3": 0, "4": 0, "total": 0}
            )
            t1_c, t2_c, t3_c, t4_c, tot = (
                counts["1"],
                counts["2"],
                counts["3"],
                counts["4"],
                counts["total"],
            )
            tot_str = f"{c_green}{tot:<5}{c_reset}" if tot > 0 else f"{c_red}0    {c_reset}"
            print(
                f"{f_code:<5} | {f_name[:45]:<45} | {t1_c:<4} | {t2_c:<4} | {t3_c:<4} | {t4_c:<4} | {tot_str}"
            )
        print("-" * 80)

        # Failures section
        if summary.failures:
            print(f"\n{c_bold}{c_red}=== FAILURES & ERRORS ({len(summary.failures)}) ==={c_reset}")
            for f in summary.failures:
                print(f"\n* {c_bold}{f.nodeid}{c_reset}")
                if f.error_message:
                    lines = f.error_message.strip().split("\n")
                    snippet = "\n  ".join(lines[-10:])
                    print(f"  {c_red}{snippet}{c_reset}")
            print("-" * 80)

    @staticmethod
    def export_json(summary: SuiteSummary, output_path: str):
        data = {
            "timestamp": summary.timestamp,
            "verdict": summary.verdict,
            "thresholds_passed": summary.thresholds_passed,
            "total_tests": summary.total_tests,
            "passed": summary.passed,
            "failed": summary.failed,
            "skipped": summary.skipped,
            "errors": summary.errors,
            "total_duration_seconds": summary.total_duration,
            "tiers": {
                tier: {
                    "name": ts.name,
                    "total": ts.total,
                    "passed": ts.passed,
                    "failed": ts.failed,
                    "skipped": ts.skipped,
                    "errors": ts.errors,
                    "duration_seconds": ts.duration,
                    "threshold_min": ts.threshold_min,
                    "passed_threshold": ts.passed_threshold,
                }
                for tier, ts in summary.tier_stats.items()
            },
            "feature_matrix": summary.feature_matrix,
            "failures": [
                {
                    "nodeid": f.nodeid,
                    "name": f.name,
                    "file_path": f.file_path,
                    "tier": f.tier,
                    "feature_id": f.feature_id,
                    "error_message": f.error_message,
                }
                for f in summary.failures
            ],
        }
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)
        print(f"[REPORT] JSON report saved to: {output_path}")

    @staticmethod
    def export_markdown(summary: SuiteSummary, output_path: str):
        lines = [
            f"# E2E Test Execution Summary Report",
            f"",
            f"- **Timestamp**: `{summary.timestamp}`",
            f"- **Verdict**: **`{summary.verdict}`**",
            f"- **Thresholds Passed**: `{summary.thresholds_passed}`",
            f"- **Total Duration**: `{summary.total_duration:.2f}s`",
            f"",
            f"## Suite Totals",
            f"",
            f"| Total Tests | Passed | Failed | Skipped | Errors | Pass Rate |",
            f"|---|---|---|---|---|---|",
            f"| {summary.total_tests} | {summary.passed} | {summary.failed} | {summary.skipped} | {summary.errors} | {(summary.passed / summary.total_tests * 100) if summary.total_tests else 0:.1f}% |",
            f"",
            f"## Tier Summary",
            f"",
            f"| Tier | Name | Target Min | Executed | Passed | Failed | Duration | Threshold Status |",
            f"|---|---|---|---|---|---|---|---|",
        ]
        for tier, ts in sorted(summary.tier_stats.items()):
            status = (
                "COLLECTED"
                if summary.verdict == "COLLECTED"
                else "MET"
                if ts.passed_threshold
                else "UNMET"
            )
            lines.append(
                f"| Tier {tier} | {ts.name} | >={ts.threshold_min} | {ts.total} | {ts.passed} | {ts.failed} | {ts.duration:.2f}s | {status} |"
            )

        lines.extend([
            f"",
            f"## Feature Coverage Matrix (29 Features)",
            f"",
            f"| ID | Feature Name | Tier 1 | Tier 2 | Tier 3 | Tier 4 | Tier 5 | Total Passed | Status |",
            f"|---|---|---|---|---|---|---|---|---|",
        ])
        for num, (f_code, f_name) in FEATURES.items():
            counts = summary.feature_matrix.get(
                f_code, {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "total": 0}
            )
            tot = counts["total"]
            status = "COVERED" if tot > 0 else "MISSING"
            lines.append(
                f"| `{f_code}` | {f_name} | {counts['1']} | {counts['2']} | {counts['3']} | {counts['4']} | {counts.get('5', 0)} | **{tot}** | {status} |"
            )

        if summary.failures:
            lines.extend([
                f"",
                f"## Failures & Diagnostics",
                f"",
            ])
            for f in summary.failures:
                lines.append(f"### `{f.nodeid}`")
                lines.append(f"- **Tier**: Tier {f.tier}")
                lines.append(f"- **Feature**: {f.feature_id or 'Unknown'}")
                lines.append(f"```text")
                lines.append(f"{f.error_message or 'Unknown error'}")
                lines.append(f"```")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fp:
            fp.write("\n".join(lines))
        print(f"[REPORT] Markdown report saved to: {output_path}")


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Local AI Gateway E2E Test Suite Runner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--tier",
        type=str,
        default="all",
        help="Test tier to execute: '1', '2', '3', '4', '5', or 'all' (can be comma-separated, e.g. '1,2,5')",
    )
    parser.add_argument(
        "--feature",
        type=str,
        default=None,
        help="Filter execution to specific feature ID (e.g. 'F7', '7', 'F24')",
    )
    parser.add_argument(
        "-k",
        "--keyword",
        type=str,
        default=None,
        help="Pytest keyword expression filter (e.g. 'streaming and not boundary')",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose test runner output",
    )
    parser.add_argument(
        "-x",
        "--fail-fast",
        action="store_true",
        help="Stop execution immediately on first test failure",
    )
    parser.add_argument(
        "--json-output",
        type=str,
        default=None,
        help="File path to export JSON summary report",
    )
    parser.add_argument(
        "--markdown-output",
        type=str,
        default=None,
        help="File path to export Markdown summary report",
    )
    parser.add_argument(
        "--max-duration",
        type=float,
        default=120.0,
        help="Maximum allowed duration in seconds for entire suite before threshold violation",
    )
    parser.add_argument(
        "--min-tests",
        type=int,
        default=None,
        help="Minimum required test count to satisfy pass threshold",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and list matching test cases without executing them",
    )

    args = parser.parse_args()

    if args.tier.lower() == "all":
        tiers = ["1", "2", "3", "4", "5"]
    else:
        tiers = [t.strip() for t in args.tier.split(",") if t.strip() in ("1", "2", "3", "4", "5")]

    feature_filter = args.feature
    if feature_filter and not feature_filter.upper().startswith("F"):
        feature_filter = f"F{feature_filter}"

    runner = E2ETestRunner(
        tiers=tiers,
        feature_filter=feature_filter,
        keyword=args.keyword,
        verbose=args.verbose,
        fail_fast=args.fail_fast,
        max_duration=args.max_duration,
        min_tests=args.min_tests,
        dry_run=args.dry_run,
    )

    summary = runner.run()

    ReportFormatter.print_terminal_report(summary)

    if args.json_output:
        ReportFormatter.export_json(summary, args.json_output)

    if args.markdown_output:
        ReportFormatter.export_markdown(summary, args.markdown_output)

    sys.exit(0 if summary.thresholds_passed else 1)


if __name__ == "__main__":
    main()
