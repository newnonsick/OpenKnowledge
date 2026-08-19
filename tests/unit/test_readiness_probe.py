from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.pool import NullPool

from src.gateway.infrastructure.readiness import ReadinessProbe


class FakeConnectionContext:
    def __init__(self, counter: list[int]):
        self.counter = counter

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def execute(self, statement):
        self.counter[0] += 1
        await asyncio.sleep(0.01)


class FakeEngine:
    def __init__(self, counter: list[int]):
        self.counter = counter
        self.disposed = False

    def connect(self):
        return FakeConnectionContext(self.counter)

    async def dispose(self):
        self.disposed = True


@pytest.mark.asyncio
async def test_readiness_probe_uses_nullpool_singleflight_and_cache():
    query_count = [0]
    pool_classes = []
    engines = []

    def engine_factory(url, **kwargs):
        pool_classes.append(kwargs.get("poolclass"))
        engine = FakeEngine(query_count)
        engines.append(engine)
        return engine

    probe = ReadinessProbe(
        "postgresql+asyncpg://db.example/gateway",
        ttl_seconds=30.0,
        engine_factory=engine_factory,
    )

    results = await asyncio.gather(*[probe.check() for _ in range(25)])
    cached_result = await probe.check()

    assert all(results)
    assert cached_result is True
    assert query_count == [1]
    assert pool_classes == [NullPool]
    assert all(engine.disposed for engine in engines)
