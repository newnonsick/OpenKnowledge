from collections import defaultdict
from threading import Lock
from time import perf_counter


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active = 0
        self._requests = defaultdict(int)
        self._duration_sum = defaultdict(float)

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
        return "\n".join(lines) + "\n"


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

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            route = getattr(scope.get("route"), "path", None) or self._fallback_route(scope.get("path", "/"))
            self.registry.finish(method, route, status, perf_counter() - started)

    @staticmethod
    def _fallback_route(path: str) -> str:
        if path in {"/health", "/healthz/live", "/healthz/ready", "/metrics", "/v1/health"}:
            return path
        parts = [part for part in path.split("/") if part]
        return "/" + "/".join(parts[:2]) + "/unmatched"
