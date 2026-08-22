from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from time import monotonic
from typing import TypeVar
from weakref import WeakKeyDictionary

from src.gateway.observability import increment_metric


T = TypeVar("T")
_guards: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, ResiliencePolicy], "UpstreamResilience"]] = WeakKeyDictionary()


class CircuitOpenError(RuntimeError):
    pass


class BulkheadRejectedError(RuntimeError):
    pass


class RetryableStatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"Retryable upstream status: {status_code}")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ResiliencePolicy:
    max_attempts: int = 3
    base_backoff_seconds: float = 0.2
    max_backoff_seconds: float = 1.0
    max_concurrency: int = 20
    bulkhead_timeout_seconds: float = 1.0
    circuit_failure_threshold: int = 5
    circuit_recovery_seconds: float = 30.0
    total_timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if (
            self.max_attempts < 1
            or self.base_backoff_seconds < 0
            or self.max_backoff_seconds < self.base_backoff_seconds
            or self.max_concurrency < 1
            or self.bulkhead_timeout_seconds <= 0
            or self.circuit_failure_threshold < 1
            or self.circuit_recovery_seconds <= 0
            or (self.total_timeout_seconds is not None and self.total_timeout_seconds <= 0)
        ):
            raise ValueError("Invalid upstream resilience policy")


class UpstreamResilience:
    def __init__(
        self,
        policy: ResiliencePolicy,
        *,
        name: str = "upstream",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._policy = policy
        self._name = name
        self._sleep = sleep
        self._clock = clock
        self._semaphore = asyncio.Semaphore(policy.max_concurrency)
        self._state_lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._opened_until = 0.0
        self._probe_in_flight = False

    async def call(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        retry_if: Callable[[Exception], bool],
    ) -> T:
        if self._policy.total_timeout_seconds is None:
            return await self._call(operation, retry_if=retry_if)
        task = asyncio.create_task(self._call(operation, retry_if=retry_if))
        try:
            done, _ = await asyncio.wait(
                {task},
                timeout=self._policy.total_timeout_seconds,
            )
        except BaseException:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise
        if task in done:
            return await task
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await self._record_failure(False)
        raise TimeoutError("Upstream operation exceeded its total deadline")

    async def _call(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        retry_if: Callable[[Exception], bool],
    ) -> T:
        async with self.lease() as lease:
            for attempt in range(self._policy.max_attempts):
                try:
                    result = await operation()
                except Exception as exc:
                    retryable = retry_if(exc)
                    if not retryable or attempt + 1 >= self._policy.max_attempts:
                        if retryable:
                            await lease.failure()
                        else:
                            await lease.success()
                        raise
                    delay = min(
                        self._policy.base_backoff_seconds * (2**attempt),
                        self._policy.max_backoff_seconds,
                    )
                    increment_metric("gateway_upstream_resilience_events_total", dependency=self._name, event="retry")
                    await self._sleep(delay)
                else:
                    await lease.success()
                    return result
            raise RuntimeError("Upstream attempt loop terminated unexpectedly")

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[_ResilienceLease]:
        probe = await self._before_call()
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._policy.bulkhead_timeout_seconds,
            )
        except TimeoutError as exc:
            await self._release_probe(probe)
            increment_metric(
                "gateway_upstream_resilience_events_total",
                dependency=self._name,
                event="bulkhead_rejected",
            )
            raise BulkheadRejectedError("Upstream concurrency capacity is exhausted") from exc
        lease = _ResilienceLease(self, probe)
        try:
            yield lease
        finally:
            await lease.neutral()
            self._semaphore.release()

    async def _before_call(self) -> bool:
        async with self._state_lock:
            now = self._clock()
            if self._opened_until > now:
                increment_metric(
                    "gateway_upstream_resilience_events_total",
                    dependency=self._name,
                    event="circuit_rejected",
                )
                raise CircuitOpenError("Upstream circuit is open")
            if self._opened_until:
                if self._probe_in_flight:
                    increment_metric(
                        "gateway_upstream_resilience_events_total",
                        dependency=self._name,
                        event="circuit_rejected",
                    )
                    raise CircuitOpenError("Upstream circuit probe is in progress")
                self._probe_in_flight = True
                return True
            return False

    async def _record_success(self) -> None:
        async with self._state_lock:
            self._consecutive_failures = 0
            self._opened_until = 0.0
            self._probe_in_flight = False
            increment_metric("gateway_upstream_resilience_events_total", dependency=self._name, event="success")

    async def _record_failure(self, probe: bool) -> None:
        async with self._state_lock:
            self._consecutive_failures += 1
            self._probe_in_flight = False
            if probe or self._consecutive_failures >= self._policy.circuit_failure_threshold:
                self._opened_until = self._clock() + self._policy.circuit_recovery_seconds
                increment_metric("gateway_upstream_resilience_events_total", dependency=self._name, event="circuit_opened")

    async def _release_probe(self, probe: bool) -> None:
        if not probe:
            return
        async with self._state_lock:
            self._probe_in_flight = False


def shared_upstream_resilience(name: str, policy: ResiliencePolicy) -> UpstreamResilience:
    loop = asyncio.get_running_loop()
    by_name = _guards.setdefault(loop, {})
    key = (name, policy)
    guard = by_name.get(key)
    if guard is None:
        guard = UpstreamResilience(policy, name=name)
        by_name[key] = guard
    return guard


class _ResilienceLease:
    def __init__(self, guard: UpstreamResilience, probe: bool) -> None:
        self._guard = guard
        self._probe = probe
        self._resolved = False

    async def success(self) -> None:
        if not self._resolved:
            self._resolved = True
            await self._guard._record_success()

    async def failure(self) -> None:
        if not self._resolved:
            self._resolved = True
            await self._guard._record_failure(self._probe)

    async def neutral(self) -> None:
        if not self._resolved:
            self._resolved = True
            await self._guard._release_probe(self._probe)
