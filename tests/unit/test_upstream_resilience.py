import asyncio

import pytest
import httpx

from src.gateway.domain.exceptions import LLMProviderException
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.adapters.http_llm_client import HttpLLMClient
from src.gateway.infrastructure.adapters.upstream_resilience import (
    BulkheadRejectedError,
    CircuitOpenError,
    ResiliencePolicy,
    UpstreamResilience,
)


async def test_retries_retryable_failure_with_bounded_backoff() -> None:
    delays: list[float] = []
    calls = 0
    guard = UpstreamResilience(
        ResiliencePolicy(max_attempts=3, base_backoff_seconds=0.1, max_backoff_seconds=0.15),
        sleep=lambda delay: _record_delay(delays, delay),
    )

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TimeoutError
        return "ok"

    assert await guard.call(operation, retry_if=lambda exc: isinstance(exc, TimeoutError)) == "ok"
    assert calls == 3
    assert delays == [0.1, 0.15]


async def test_non_retryable_failure_is_not_repeated() -> None:
    calls = 0
    guard = UpstreamResilience(ResiliencePolicy(max_attempts=3))

    async def operation() -> None:
        nonlocal calls
        calls += 1
        raise ValueError

    with pytest.raises(ValueError):
        await guard.call(operation, retry_if=lambda exc: isinstance(exc, TimeoutError))
    assert calls == 1


async def test_circuit_opens_after_consecutive_failed_operations() -> None:
    guard = UpstreamResilience(
        ResiliencePolicy(max_attempts=1, circuit_failure_threshold=2, circuit_recovery_seconds=30)
    )

    async def operation() -> None:
        raise TimeoutError

    for _ in range(2):
        with pytest.raises(TimeoutError):
            await guard.call(operation, retry_if=lambda exc: True)
    with pytest.raises(CircuitOpenError):
        await guard.call(operation, retry_if=lambda exc: True)


async def test_operation_timeout_counts_as_one_failed_operation() -> None:
    guard = UpstreamResilience(
        ResiliencePolicy(
            max_attempts=1,
            circuit_failure_threshold=2,
            total_timeout_seconds=1,
        )
    )

    async def operation() -> None:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        await guard.call(operation, retry_if=lambda exc: True)
    with pytest.raises(TimeoutError):
        await guard.call(operation, retry_if=lambda exc: True)
    with pytest.raises(CircuitOpenError):
        await guard.call(operation, retry_if=lambda exc: True)


async def test_bulkhead_rejects_when_capacity_is_exhausted() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    guard = UpstreamResilience(
        ResiliencePolicy(max_concurrency=1, bulkhead_timeout_seconds=0.01)
    )

    async def blocking() -> None:
        entered.set()
        await release.wait()

    task = asyncio.create_task(guard.call(blocking, retry_if=lambda exc: False))
    await entered.wait()
    with pytest.raises(BulkheadRejectedError):
        await guard.call(blocking, retry_if=lambda exc: False)
    release.set()
    await task


async def test_embedding_adapter_retries_transient_status() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        embedding = HTTPEmbeddingClient(
            base_url="https://embedding.test/v1",
            model_id="embed",
            dimension=2,
            http_client=client,
            resilience_policy=ResiliencePolicy(max_attempts=2, base_backoff_seconds=0, max_backoff_seconds=0),
        )
        assert await embedding.embed_query("family") == [0.1, 0.2]
    assert attempts == 2


async def test_stream_adapter_retries_only_before_stream_opens() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        body = 'data: {"id":"one","model":"test","choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        llm = HttpLLMClient(
            base_url="https://llm.test/v1",
            api_key="key",
            default_model="test",
            client=client,
            resilience_policy=ResiliencePolicy(max_attempts=2, base_backoff_seconds=0, max_backoff_seconds=0),
        )
        chunks = [chunk async for chunk in llm.generate_stream([{"role": "user", "content": "hello"}])]
    assert [chunk.delta_content for chunk in chunks] == ["ok"]
    assert attempts == 2


async def test_non_stream_llm_adapter_retries_transient_status() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "id": "one",
                "model": "test",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        llm = HttpLLMClient(
            base_url="https://llm.test/v1",
            api_key="key",
            default_model="test",
            client=client,
            resilience_policy=ResiliencePolicy(max_attempts=2, base_backoff_seconds=0, max_backoff_seconds=0),
        )
        response = await llm.generate([{"role": "user", "content": "hello"}])
    assert response.content == "ok"
    assert attempts == 2


async def test_llm_adapter_does_not_retry_client_rejection() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        llm = HttpLLMClient(
            base_url="https://llm.test/v1",
            api_key="key",
            default_model="test",
            client=client,
            resilience_policy=ResiliencePolicy(max_attempts=3, base_backoff_seconds=0, max_backoff_seconds=0),
        )
        with pytest.raises(LLMProviderException):
            await llm.generate([{"role": "user", "content": "hello"}])
    assert attempts == 1


async def test_stream_adapter_never_retries_after_emitting_data() -> None:
    attempts = 0

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"one","model":"test","choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError("stream interrupted")

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(200, stream=BrokenStream(), headers={"content-type": "text/event-stream"})

    chunks = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        llm = HttpLLMClient(
            base_url="https://llm.test/v1",
            api_key="key",
            default_model="test",
            client=client,
            resilience_policy=ResiliencePolicy(max_attempts=3, base_backoff_seconds=0, max_backoff_seconds=0),
        )
        with pytest.raises(LLMProviderException):
            async for chunk in llm.generate_stream([{"role": "user", "content": "hello"}]):
                chunks.append(chunk)
    assert [chunk.delta_content for chunk in chunks] == ["partial"]
    assert attempts == 1


async def _record_delay(delays: list[float], delay: float) -> None:
    delays.append(delay)
