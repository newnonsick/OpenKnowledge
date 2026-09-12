from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.gateway.infrastructure.persistence.ingestion_models import IngestionJobModel
from src.gateway.presentation.metrics import MetricsRegistry

logger = logging.getLogger(__name__)

QUEUE_STATES = ("queued", "retry_wait", "running")


class OperationalMetricsCollector:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        registry: MetricsRegistry,
        *,
        interval_seconds: float,
        metrics_file: Path | None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Metrics collection interval must be positive")
        self._session_factory = session_factory
        self._registry = registry
        self._interval_seconds = interval_seconds
        self._metrics_file = metrics_file

    async def run_once(self) -> dict[str, int]:
        async with self._session_factory.begin() as session:
            rows = await session.execute(
                select(IngestionJobModel.state, func.count(IngestionJobModel.id))
                .where(IngestionJobModel.state.in_(QUEUE_STATES))
                .group_by(IngestionJobModel.state)
            )
            counts = {state: int(count) for state, count in rows}
            depths = {state: int(counts.get(state, 0)) for state in QUEUE_STATES}
        for state, depth in depths.items():
            self._registry.set_gauge("gateway_ingestion_queue_depth", depth, state=state)
        self._registry.set_gauge("gateway_dependency_available", 1, dependency="database")
        if self._metrics_file is not None:
            self._publish()
        return depths

    def _publish(self) -> None:
        assert self._metrics_file is not None
        self._metrics_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._metrics_file.with_name(f".{self._metrics_file.name}.tmp")
        temporary.write_text(self._registry.render(), encoding="utf-8")
        temporary.replace(self._metrics_file)

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Operational metrics collection failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                pass
