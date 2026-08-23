from __future__ import annotations

import json
import logging
from uuid import uuid4

import httpx
import pytest

from src.gateway.config import RuntimeEnvironment, Settings
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.main import create_app
from src.gateway.observability import (
    ConsoleLogFormatter,
    JsonLogFormatter,
    configure_logging,
    metrics_registry_context,
    trace_id_context,
    traceparent_context,
)
from src.gateway.presentation.metrics import MetricsRegistry
from src.gateway.presentation.request_context import request_id_context


def test_structured_logs_include_correlation_and_drop_sensitive_fields() -> None:
    request_id = str(uuid4())
    request_token = request_id_context.set(request_id)
    trace_token = trace_id_context.set("a" * 32)
    try:
        record = logging.LogRecord("gateway.test", logging.INFO, __file__, 10, "request completed", (), None)
        record.path = "/api/v1/knowledge"
        record.outcome = "success"
        record.authorization = "Bearer sentinel-secret"
        record.content = "private family document"
        payload = json.loads(JsonLogFormatter().format(record))
    finally:
        trace_id_context.reset(trace_token)
        request_id_context.reset(request_token)

    assert payload["event"] == "request completed"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "gateway.test"
    assert payload["outcome"] == "success"
    assert payload["path"] == "/api/v1/knowledge"
    assert payload["request_id"] == request_id
    assert payload["trace_id"] == "a" * 32
    assert payload["timestamp"].endswith("+00:00")
    assert "sentinel-secret" not in json.dumps(payload)
    assert "private family document" not in json.dumps(payload)


def test_console_logs_are_scannable_and_redacted() -> None:
    request_id = str(uuid4())
    request_token = request_id_context.set(request_id)
    trace_token = trace_id_context.set("a" * 32)
    try:
        record = logging.LogRecord(
            "gateway.request",
            logging.WARNING,
            __file__,
            10,
            "Request verification failed",
            (),
            None,
        )
        record.method = "POST"
        record.route = "/api/v1/auth/password"
        record.status = 403
        record.duration_ms = 12.5
        rendered = ConsoleLogFormatter().format(record)
    finally:
        trace_id_context.reset(trace_token)
        request_id_context.reset(request_token)

    assert "WARNING" in rendered
    assert "gateway.request" in rendered
    assert "Request verification failed" in rendered
    assert "method=POST" in rendered
    assert "route=/api/v1/auth/password" in rendered
    assert "status=403" in rendered
    assert "duration_ms=12.5" in rendered
    assert f"request_id={request_id}" in rendered
    assert f"trace_id={'a' * 32}" in rendered


def test_json_logs_include_redacted_exception_details() -> None:
    try:
        raise ValueError("database endpoint contains bearer sentinel-secret")
    except ValueError as exc:
        record = logging.LogRecord(
            "gateway.test",
            logging.ERROR,
            __file__,
            10,
            "Operation failed",
            (),
            exc_info=(ValueError, exc, exc.__traceback__),
        )
    payload = json.loads(JsonLogFormatter().format(record))

    assert payload["exception_class"] == "ValueError"
    assert "database endpoint" in payload["exception"]
    assert "sentinel-secret" not in payload["exception"]


def test_configure_logging_selects_environment_format_and_removes_duplicate_access_logs() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    logger_names = ("uvicorn", "uvicorn.access", "uvicorn.error")
    original_states = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
        )
        for name in logger_names
    }

    try:
        configure_logging("INFO", log_format="auto", environment=RuntimeEnvironment.DEVELOPMENT)
        assert isinstance(root.handlers[0].formatter, ConsoleLogFormatter)
        assert logging.getLogger("uvicorn.access").propagate is False

        configure_logging("INFO", log_format="auto", environment=RuntimeEnvironment.PRODUCTION)
        assert isinstance(root.handlers[0].formatter, JsonLogFormatter)

        configure_logging("INFO", log_format="console")
        assert isinstance(root.handlers[0].formatter, ConsoleLogFormatter)
    finally:
        root.handlers.clear()
        root.handlers.extend(original_handlers)
        root.setLevel(original_level)
        for name in logger_names:
            target = logging.getLogger(name)
            handlers, propagate, disabled = original_states[name]
            target.handlers.clear()
            target.handlers.extend(handlers)
            target.propagate = propagate
            target.disabled = disabled


def test_operational_metrics_render_required_low_cardinality_families() -> None:
    registry = MetricsRegistry()
    registry.increment("gateway_auth_events_total", event="login", outcome="success")
    registry.increment("gateway_tool_events_total", event="confirmation", outcome="success")
    registry.observe("gateway_database_query_duration_seconds", 0.025, operation="select", outcome="success")
    registry.observe("gateway_dependency_duration_seconds", 0.4, dependency="embedding", operation="batch", outcome="success")
    registry.observe("gateway_retrieval_duration_seconds", 0.08, phase="fusion", outcome="success")
    registry.set_gauge("gateway_ingestion_queue_depth", 3, state="queued")

    rendered = registry.render()

    assert 'gateway_auth_events_total{event="login",outcome="success"} 1' in rendered
    assert 'gateway_tool_events_total{event="confirmation",outcome="success"} 1' in rendered
    assert 'gateway_database_query_duration_seconds_count{operation="select",outcome="success"} 1' in rendered
    assert 'gateway_dependency_duration_seconds_sum{dependency="embedding",operation="batch",outcome="success"} 0.400000000' in rendered
    assert 'gateway_retrieval_duration_seconds_count{phase="fusion",outcome="success"} 1' in rendered
    assert 'gateway_ingestion_queue_depth{state="queued"} 3' in rendered
    assert "request_id" not in rendered

    with pytest.raises(ValueError):
        registry.increment("gateway_auth_events_total", event="login", outcome="success", member_id=str(uuid4()))


@pytest.mark.asyncio
async def test_request_trace_context_is_validated_and_propagated() -> None:
    settings = Settings(
        gateway={
            "environment": "test",
            "api_keys": ["test-key"],
            "legacy_api_keys_enabled": True,
            "cors_origins": ["http://localhost:3000"],
            "trusted_hosts": ["testserver"],
        }
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    supplied = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        propagated = await client.get("/healthz/live", headers={"traceparent": supplied})
        replaced = await client.get("/healthz/live", headers={"traceparent": "not-valid"})

    assert propagated.headers["traceparent"].startswith("00-0123456789abcdef0123456789abcdef-")
    assert propagated.headers["traceparent"].endswith("-01")
    assert replaced.headers["traceparent"].startswith("00-")
    assert replaced.headers["traceparent"] != "not-valid"
    assert len(replaced.headers["traceparent"]) == 55


@pytest.mark.asyncio
async def test_upstream_calls_carry_correlation_and_emit_dependency_metrics() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    registry = MetricsRegistry()
    metrics_token = metrics_registry_context.set(registry)
    request_token = request_id_context.set("request-correlation")
    trace_token = trace_id_context.set("b" * 32)
    traceparent_token = traceparent_context.set("00-" + "b" * 32 + "-" + "c" * 16 + "-01")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        embeddings = await HTTPEmbeddingClient(
            base_url="https://embedding.example/v1",
            api_key="provider-secret",
            dimension=2,
            http_client=client,
        ).embed_texts(["family content"])
    finally:
        await client.aclose()
        traceparent_context.reset(traceparent_token)
        trace_id_context.reset(trace_token)
        request_id_context.reset(request_token)
        metrics_registry_context.reset(metrics_token)

    assert embeddings == [[0.1, 0.2]]
    assert captured["x-request-id"] == "request-correlation"
    assert captured["traceparent"].startswith("00-" + "b" * 32 + "-")
    assert captured["traceparent"].endswith("-01")
    assert captured["traceparent"] != "00-" + "b" * 32 + "-" + "c" * 16 + "-01"
    rendered = registry.render()
    assert 'gateway_embedding_events_total{event="batch",outcome="success"} 1' in rendered
    assert 'gateway_dependency_duration_seconds_count{dependency="embedding",operation="batch",outcome="success"} 1' in rendered
    assert 'gateway_embedding_batch_size_count{operation="batch",outcome="success"} 1' in rendered
    assert 'gateway_upstream_resilience_events_total{dependency="embedding",event="success"} 1' in rendered
