import asyncio

import pytest

from src.gateway.application.services.document_ingestion_worker import DocumentIngestionWorker
from src.gateway.domain.exceptions import JobLeaseLostException


def _build_worker() -> DocumentIngestionWorker:
    return DocumentIngestionWorker(
        session_factory=object(),
        storage=object(),
        parser=object(),
        embedding_client=object(),
        worker_id="unit-worker",
        lease_seconds=30,
        heartbeat_interval_seconds=1,
        chunk_size=100,
        chunk_overlap=10,
        max_chunks=5,
    )


async def test_run_until_stopped_survives_single_lease_loss():
    worker = _build_worker()
    stop_event = asyncio.Event()
    calls = {"count": 0}

    async def flaky_run_once():
        calls["count"] += 1
        if calls["count"] == 1:
            raise JobLeaseLostException()
        stop_event.set()
        return None

    worker.run_once = flaky_run_once
    await asyncio.wait_for(
        worker.run_until_stopped(stop_event, idle_delay_seconds=0.01),
        timeout=2,
    )
    assert calls["count"] >= 2


async def test_run_until_stopped_survives_repeated_lease_losses():
    worker = _build_worker()
    stop_event = asyncio.Event()
    calls = {"count": 0}

    async def always_leaking_run_once():
        calls["count"] += 1
        if calls["count"] < 3:
            raise JobLeaseLostException()
        stop_event.set()
        return None

    worker.run_once = always_leaking_run_once
    await asyncio.wait_for(
        worker.run_until_stopped(stop_event, idle_delay_seconds=0.01),
        timeout=2,
    )
    assert calls["count"] == 3


async def test_run_until_stopped_propagates_unexpected_errors():
    worker = _build_worker()
    stop_event = asyncio.Event()

    async def broken_run_once():
        raise RuntimeError("boom")

    worker.run_once = broken_run_once
    with pytest.raises(RuntimeError):
        await worker.run_until_stopped(stop_event, idle_delay_seconds=0.01)
