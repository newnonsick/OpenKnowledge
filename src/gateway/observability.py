from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import re
from secrets import token_hex
from typing import Any


trace_id_context: ContextVar[str | None] = ContextVar("trace_id", default=None)
traceparent_context: ContextVar[str | None] = ContextVar("traceparent", default=None)
metrics_registry_context: ContextVar[Any | None] = ContextVar("metrics_registry", default=None)

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_BEARER = re.compile(r"(?i)bearer\s+[^\s,;]+")
_URL_CREDENTIAL = re.compile(r"(://[^:/\s]+:)[^@/\s]+(@)")
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(password|secret|token|api[_-]?key)\s*[:=]\s*[^\s,;]+")
_SAFE_FIELDS = frozenset(
    {
        "attempt",
        "deduplication_key",
        "dependency",
        "duration_ms",
        "error_code",
        "event_type",
        "exception_class",
        "job_id",
        "key_id",
        "member_id",
        "method",
        "operation",
        "outcome",
        "path",
        "route",
        "space_id",
        "state",
        "status",
        "status_code",
        "tool_name",
        "worker_id",
    }
)


def create_traceparent(value: str | None) -> tuple[str, str]:
    match = _TRACEPARENT.fullmatch((value or "").lower())
    if match and match.group(1) != "0" * 32 and match.group(2) != "0" * 16:
        trace_id = match.group(1)
        flags = match.group(3)
    else:
        trace_id = token_hex(16)
        flags = "01"
    return trace_id, f"00-{trace_id}-{token_hex(8)}-{flags}"


def current_trace_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    traceparent = traceparent_context.get()
    if traceparent:
        match = _TRACEPARENT.fullmatch(traceparent)
        headers["traceparent"] = (
            f"00-{match.group(1)}-{token_hex(8)}-{match.group(3)}"
            if match
            else traceparent
        )
    try:
        from src.gateway.presentation.request_context import request_id_context

        request_id = request_id_context.get()
    except ImportError:
        request_id = None
    if request_id:
        headers["X-Request-ID"] = request_id
    return headers


def increment_metric(name: str, amount: float = 1, **labels: str) -> None:
    registry = metrics_registry_context.get()
    if registry is not None:
        registry.increment(name, amount, **labels)


def observe_metric(name: str, value: float, **labels: str) -> None:
    registry = metrics_registry_context.get()
    if registry is not None:
        registry.observe(name, value, **labels)


def set_metric_gauge(name: str, value: float, **labels: str) -> None:
    registry = metrics_registry_context.get()
    if registry is not None:
        registry.set_gauge(name, value, **labels)


def _redact(value: str) -> str:
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _URL_CREDENTIAL.sub(r"\1[REDACTED]\2", value)
    return _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            from src.gateway.presentation.request_context import request_id_context

            request_id = request_id_context.get()
        except ImportError:
            request_id = None
        payload: dict[str, object] = {
            "event": _redact(record.getMessage()),
            "level": record.levelname,
            "logger": record.name,
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
        }
        if request_id:
            payload["request_id"] = request_id
        trace_id = trace_id_context.get()
        if trace_id:
            payload["trace_id"] = trace_id
        for field in _SAFE_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = _redact(str(value))
        if record.exc_info:
            payload["exception_class"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.handlers.clear()
    root.addHandler(handler)
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        target = logging.getLogger(name)
        target.handlers.clear()
        target.propagate = True
    for name in ("httpcore", "httpx"):
        logging.getLogger(name).setLevel(logging.WARNING)
