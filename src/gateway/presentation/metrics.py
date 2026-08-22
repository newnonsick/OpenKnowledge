from collections import defaultdict
import logging
from threading import Lock
from time import perf_counter

from src.gateway.observability import metrics_registry_context, trace_id_context
from src.gateway.presentation.request_context import request_id_context


logger = logging.getLogger(__name__)


_COUNTER_LABELS = {
    "gateway_auth_events_total": ("event", "outcome"),
    "gateway_database_transactions_total": ("outcome",),
    "gateway_embedding_events_total": ("event", "outcome"),
    "gateway_ingestion_events_total": ("event", "outcome"),
    "gateway_llm_events_total": ("event", "outcome"),
    "gateway_retrieval_events_total": ("event", "outcome"),
    "gateway_tool_events_total": ("event", "outcome"),
    "gateway_upstream_resilience_events_total": ("dependency", "event"),
}

_OBSERVATION_LABELS = {
    "gateway_database_acquisition_duration_seconds": ("outcome",),
    "gateway_database_query_duration_seconds": ("operation", "outcome"),
    "gateway_dependency_duration_seconds": ("dependency", "operation", "outcome"),
    "gateway_embedding_batch_size": ("operation", "outcome"),
    "gateway_embedding_generation_coverage_ratio": ("outcome",),
    "gateway_ingestion_claim_latency_seconds": ("outcome",),
    "gateway_ingestion_processing_duration_seconds": ("outcome",),
    "gateway_llm_token_count": ("direction", "outcome"),
    "gateway_retrieval_citation_coverage_ratio": ("outcome",),
    "gateway_retrieval_duration_seconds": ("phase", "outcome"),
    "gateway_retrieval_result_count": ("outcome",),
    "gateway_tool_iteration_count": ("outcome",),
}

_GAUGE_LABELS = {
    "gateway_backup_age_seconds": (),
    "gateway_certificate_expiry_seconds": (),
    "gateway_database_pool_connections": ("state",),
    "gateway_dependency_available": ("dependency",),
    "gateway_ingestion_queue_depth": ("state",),
    "gateway_replication_lag_seconds": ("kind",),
    "gateway_restore_test_age_seconds": (),
    "gateway_storage_bytes": ("kind",),
}


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active = 0
        self._requests = defaultdict(int)
        self._duration_sum = defaultdict(float)
        self._counters = defaultdict(float)
        self._observations = defaultdict(lambda: [0, 0.0])
        self._gauges = {}

    @staticmethod
    def _labels(name: str, labels: dict[str, str], allowed: dict[str, tuple[str, ...]]) -> tuple[tuple[str, str], ...]:
        expected = allowed.get(name)
        if expected is None or set(labels) != set(expected):
            raise ValueError("Unsupported metric or label set")
        normalized = tuple((key, str(labels[key])) for key in expected)
        if any(len(value) > 48 or "\n" in value or '"' in value for _, value in normalized):
            raise ValueError("Metric label value is unsafe")
        return normalized

    def increment(self, name: str, amount: float = 1, **labels: str) -> None:
        normalized = self._labels(name, labels, _COUNTER_LABELS)
        with self._lock:
            self._counters[(name, normalized)] += amount

    def observe(self, name: str, value: float, **labels: str) -> None:
        if value < 0:
            raise ValueError("Metric observations must not be negative")
        normalized = self._labels(name, labels, _OBSERVATION_LABELS)
        with self._lock:
            current = self._observations[(name, normalized)]
            current[0] += 1
            current[1] += value

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        normalized = self._labels(name, labels, _GAUGE_LABELS)
        with self._lock:
            self._gauges[(name, normalized)] = value

    def start(self) -> None:
        with self._lock:
            self._active += 1

    def finish(self, method: str, route: str, status: int, duration: float) -> None:
        key = (method, route, status)
        with self._lock:
            self._active -= 1
            self._requests[key] += 1
            self._duration_sum[(method, route)] += duration

    def render(self) -> str:
        with self._lock:
            active = self._active
            requests = dict(self._requests)
            durations = dict(self._duration_sum)
            counters = dict(self._counters)
            observations = {key: tuple(value) for key, value in self._observations.items()}
            gauges = dict(self._gauges)
        lines = [
            "# TYPE gateway_http_active_requests gauge",
            f"gateway_http_active_requests {active}",
            "# TYPE gateway_http_requests_total counter",
        ]
        for (method, route, status), count in sorted(requests.items()):
            lines.append(
                f'gateway_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}'
            )
        lines.append("# TYPE gateway_http_request_duration_seconds_sum counter")
        for (method, route), duration in sorted(durations.items()):
            lines.append(
                f'gateway_http_request_duration_seconds_sum{{method="{method}",route="{route}"}} {duration:.9f}'
            )
        metric_types: set[str] = set()
        for (name, labels), value in sorted(counters.items()):
            if name not in metric_types:
                lines.append(f"# TYPE {name} counter")
                metric_types.add(name)
            lines.append(f"{name}{self._render_labels(labels)} {self._number(value)}")
        for (name, labels), (count, total) in sorted(observations.items()):
            if name not in metric_types:
                lines.append(f"# TYPE {name} histogram")
                metric_types.add(name)
            rendered_labels = self._render_labels(labels)
            lines.append(f"{name}_count{rendered_labels} {count}")
            lines.append(f"{name}_sum{rendered_labels} {total:.9f}")
        for (name, labels), value in sorted(gauges.items()):
            if name not in metric_types:
                lines.append(f"# TYPE {name} gauge")
                metric_types.add(name)
            lines.append(f"{name}{self._render_labels(labels)} {self._number(value)}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        return "{" + ",".join(f'{key}="{value}"' for key, value in labels) + "}"

    @staticmethod
    def _number(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else f"{value:.9f}"


class MetricsMiddleware:
    def __init__(self, app, *, registry: MetricsRegistry) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper()
        status = 500
        started = perf_counter()
        self.registry.start()
        metrics_token = metrics_registry_context.set(self.registry)

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            route = getattr(scope.get("route"), "path", None) or self._fallback_route(scope.get("path", "/"))
            duration = perf_counter() - started
            self.registry.finish(method, route, status, duration)
            auth_event = self._auth_event(route)
            if auth_event is not None:
                if status == 429:
                    outcome = "throttled"
                elif status < 400:
                    outcome = "success"
                else:
                    outcome = "failure"
                self.registry.increment("gateway_auth_events_total", event=auth_event, outcome=outcome)
            state = scope.get("state", {})
            request_token = request_id_context.set(state.get("request_id"))
            trace_token = trace_id_context.set(state.get("trace_id"))
            try:
                logger.info(
                    "HTTP request completed",
                    extra={
                        "duration_ms": round(duration * 1000, 3),
                        "method": method,
                        "outcome": "success" if status < 400 else "failure",
                        "route": route,
                        "status": status,
                    },
                )
            finally:
                trace_id_context.reset(trace_token)
                request_id_context.reset(request_token)
            metrics_registry_context.reset(metrics_token)

    @staticmethod
    def _fallback_route(path: str) -> str:
        if path in {"/health", "/healthz/live", "/healthz/ready", "/metrics", "/v1/health"}:
            return path
        parts = [part for part in path.split("/") if part]
        return "/" + "/".join(parts[:2]) + "/unmatched"

    @staticmethod
    def _auth_event(route: str) -> str | None:
        return {
            "/api/v1/auth/login": "login",
            "/api/v1/auth/logout": "logout",
            "/api/v1/auth/mfa/totp/confirm": "mfa",
            "/api/v1/auth/refresh": "refresh",
        }.get(route)
