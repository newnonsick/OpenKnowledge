from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from src.gateway.application.services.retention_service import RetentionMaintenanceRunner, RetentionResult
from src.gateway.config import GatewaySettings


class _RecoveringRetentionService:
    def __init__(self, stop_event: asyncio.Event) -> None:
        self.calls = 0
        self._stop_event = stop_event

    async def purge(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient maintenance failure")
        self._stop_event.set()
        return None


async def test_retention_runner_isolates_iteration_failure_and_continues() -> None:
    stop_event = asyncio.Event()
    service = _RecoveringRetentionService(stop_event)
    runner = RetentionMaintenanceRunner(
        service,
        interval_seconds=0.001,
        archive_days=365,
        revision_days=1095,
        operational_days=30,
        batch_size=100,
        max_batches_per_cycle=100,
    )
    await asyncio.wait_for(runner.run_until_stopped(stop_event), timeout=1)
    assert service.calls == 2


class _BackloggedRetentionService:
    def __init__(self) -> None:
        self.calls = 0

    async def purge(self, **kwargs):
        self.calls += 1
        if self.calls <= 3:
            return RetentionResult({"retrieval_unit": 100}, (), ())
        return RetentionResult({}, (), ())


async def test_retention_runner_drains_bounded_batches_before_sleeping() -> None:
    service = _BackloggedRetentionService()
    runner = RetentionMaintenanceRunner(
        service,
        interval_seconds=86400,
        archive_days=365,
        revision_days=1095,
        operational_days=30,
        batch_size=100,
        max_batches_per_cycle=100,
    )
    result = await runner.run_cycle()
    assert service.calls == 4
    assert result is not None
    assert result.purged_counts == {"retrieval_unit": 300}


async def test_retention_cycle_observes_shutdown_between_batches() -> None:
    stop_event = asyncio.Event()

    class _StoppingService:
        def __init__(self) -> None:
            self.calls = 0

        async def purge(self, **kwargs):
            self.calls += 1
            stop_event.set()
            return RetentionResult({"retrieval_unit": 100}, (), ())

    service = _StoppingService()
    runner = RetentionMaintenanceRunner(
        service,
        interval_seconds=86400,
        archive_days=365,
        revision_days=1095,
        operational_days=30,
        batch_size=100,
        max_batches_per_cycle=100,
    )
    result = await runner.run_cycle(stop_event)
    assert service.calls == 1
    assert result is not None
    assert result.purged_counts == {"retrieval_unit": 100}


def test_retention_settings_reject_shorter_than_database_safety_floor() -> None:
    for field, value in (
        ("retention_archive_days", 364),
        ("retention_revision_days", 1094),
        ("retention_operational_days", 29),
    ):
        with pytest.raises(ValidationError):
            GatewaySettings(**{field: value})
