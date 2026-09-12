import tests.e2e.harness.test_env  # noqa: F401  (registers SQLite type compilers)

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import pytest_asyncio

from src.gateway.application.services.operational_metrics_collector import OperationalMetricsCollector
from src.gateway.infrastructure.persistence.identity_models import MemberModel
from src.gateway.infrastructure.persistence.ingestion_models import Base as IngestionBase
from src.gateway.infrastructure.persistence.ingestion_models import IngestionJobModel
from src.gateway.infrastructure.persistence.identity_models import Base as IdentityBase
from src.gateway.infrastructure.persistence.models import Base as KnowledgeBase
from src.gateway.presentation.metrics import MetricsRegistry


@pytest_asyncio.fixture()
async def collector_factory(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)

    async with engine.begin() as connection:
        await connection.run_sync(IdentityBase.metadata.create_all)
        await connection.run_sync(KnowledgeBase.metadata.create_all)
        await connection.run_sync(IngestionBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    member_id = uuid4()
    async with factory.begin() as session:
        session.add(
            MemberModel(
                id=member_id,
                username="collector",
                username_normalized="collector",
                display_name="Collector",
                status="active",
                system_role="member",
                force_password_change=False,
            )
        )
        now = datetime.now(timezone.utc)
        for index, state in enumerate(("queued", "queued", "running", "succeeded")):
            job = IngestionJobModel(
                space_id="global",
                document_id=uuid4(),
                document_revision_id=uuid4(),
                initiated_by_member_id=member_id,
                state=state,
                idempotency_key=f"collector-{index}",
                created_at=now,
                updated_at=now,
            )
            if state == "running":
                job.lease_owner = "collector-worker"
                job.lease_expires_at = now
                job.claim_token = uuid4()
            if state == "succeeded":
                job.progress = 100
                job.finished_at = now
            session.add(job)
    try:
        yield factory, tmp_path
    finally:
        await engine.dispose()


async def test_collector_reports_global_depths_and_publishes_atomically(collector_factory) -> None:
    factory, tmp_path = collector_factory
    registry = MetricsRegistry()
    metrics_file = tmp_path / "worker.prom"
    collector = OperationalMetricsCollector(
        factory,
        registry,
        interval_seconds=60,
        metrics_file=metrics_file,
    )

    depths = await collector.run_once()

    assert depths == {"queued": 2, "retry_wait": 0, "running": 1}
    rendered = registry.render()
    assert 'gateway_ingestion_queue_depth{state="queued"} 2' in rendered
    assert 'gateway_ingestion_queue_depth{state="running"} 1' in rendered
    assert 'gateway_dependency_available{dependency="database"} 1' in rendered
    assert metrics_file.read_text(encoding="utf-8") == rendered


async def test_collector_supports_registry_only_mode(collector_factory) -> None:
    factory, _ = collector_factory
    registry = MetricsRegistry()
    collector = OperationalMetricsCollector(factory, registry, interval_seconds=60, metrics_file=None)

    depths = await collector.run_once()

    assert depths["queued"] == 2
    assert 'gateway_ingestion_queue_depth{state="queued"} 2' in registry.render()


async def test_collector_run_until_stopped_collects_and_sleeps(collector_factory) -> None:
    factory, tmp_path = collector_factory
    registry = MetricsRegistry()
    collector = OperationalMetricsCollector(
        factory,
        registry,
        interval_seconds=0.01,
        metrics_file=tmp_path / "worker.prom",
    )
    stop_event = asyncio.Event()

    async def _stop_soon() -> None:
        for _ in range(50):
            if 'gateway_ingestion_queue_depth{state="queued"} 2' in registry.render():
                stop_event.set()
                return
            await asyncio.sleep(0.01)
        stop_event.set()

    await asyncio.wait_for(asyncio.gather(collector.run_until_stopped(stop_event), _stop_soon()), timeout=5)
    assert 'gateway_ingestion_queue_depth{state="queued"} 2' in registry.render()


def test_alerts_cover_worker_metrics_absence() -> None:
    alerts = Path("deploy/prometheus/alerts.yml").read_text(encoding="utf-8")
    assert "GatewayWorkerMetricsMissing" in alerts
    assert "absent(gateway_ingestion_queue_depth)" in alerts


def test_worker_entrypoint_installs_registry_and_collector() -> None:
    worker = Path("src/gateway/worker.py").read_text(encoding="utf-8")
    assert "MetricsRegistry()" in worker
    assert "metrics_registry_context.set(registry)" in worker
    assert "OperationalMetricsCollector(" in worker
    assert "operational_metrics.run_until_stopped(stopping)" in worker


def test_operations_summary_no_longer_writes_global_gauges() -> None:
    management = Path("src/gateway/presentation/routers/management.py").read_text(encoding="utf-8")
    assert "gateway_ingestion_queue_depth" not in management
    assert "gateway_storage_bytes" not in management
    assert "gateway_dependency_available" not in management
