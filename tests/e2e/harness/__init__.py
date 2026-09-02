"""
E2E Test Harness Package for OpenKnowledge.

Exports core test infrastructure components:
- Upstream Mock Server (LLM and Embeddings)
- Test Environment & Database Compatibility Layer
- Standalone E2E Test Runner & Pytest Result Collector
"""

from importlib import import_module

from tests.e2e.harness.mock_server import (
    DeterministicEmbeddingEngine,
    MockEmbeddingController,
    MockLLMController,
    MockLLMResponse,
    MockServerManager,
    MockToolCall,
    RecordedRequest,
    create_mock_upstream_app,
)
from tests.e2e.harness.test_env import (
    TestEnvironment,
    clean_database_tables,
    detect_database_configuration,
    parse_sse_stream,
)

_RUNNER_EXPORTS = {
    "FEATURES",
    "TIER_DIRS",
    "TIER_NAMES",
    "TIER_THRESHOLDS",
    "E2ETestRunner",
    "PytestResultCollector",
    "ReportFormatter",
    "SuiteSummary",
    "TestResultItem",
    "TierStatistics",
}


def __getattr__(name: str):
    if name in _RUNNER_EXPORTS:
        return getattr(import_module("tests.e2e.harness.runner"), name)
    raise AttributeError(name)

__all__ = [
    # Mock Server
    "DeterministicEmbeddingEngine",
    "MockEmbeddingController",
    "MockLLMController",
    "MockLLMResponse",
    "MockServerManager",
    "MockToolCall",
    "RecordedRequest",
    "create_mock_upstream_app",
    # Test Environment
    "TestEnvironment",
    "clean_database_tables",
    "detect_database_configuration",
    "parse_sse_stream",
    # Runner
    "E2ETestRunner",
    "PytestResultCollector",
    "ReportFormatter",
    "SuiteSummary",
    "TestResultItem",
    "TierStatistics",
    "FEATURES",
    "TIER_DIRS",
    "TIER_NAMES",
    "TIER_THRESHOLDS",
]
