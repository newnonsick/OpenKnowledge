from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from uuid import uuid4

from sqlalchemy.engine import make_url

from src.gateway.application.services.bounded_document_parser import BoundedDocumentParser
from src.gateway.application.services.document_ingestion_worker import DocumentIngestionWorker
from src.gateway.application.services.job_outbox_dispatcher import JobOutboxDispatcher
from src.gateway.application.services.retention_service import RetentionMaintenanceRunner, RetentionService
from src.gateway.application.services.storage_consistency_service import StorageConsistencyService
from src.gateway.application.services.storage_maintenance_runner import StorageMaintenanceRunner
from src.gateway.config import get_settings
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.database import close_db_engine, get_worker_session_factory, normalize_database_url, validate_worker_database_role
from src.gateway.infrastructure.migrations import get_schema_status_async
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from src.gateway.observability import configure_logging


logger = logging.getLogger(__name__)


async def _publish_internal_event(event_type: str, payload: dict, deduplication_key: str) -> None:
    if event_type not in {
        "ingestion.queued",
        "ingestion.succeeded",
        "storage.integrity_failed",
    }:
        raise ValueError("Unsupported internal outbox event")
    logger.info(
        "Internal outbox event delivered",
        extra={"event_type": event_type, "deduplication_key": deduplication_key},
    )


async def run_worker(stop_event: asyncio.Event | None = None) -> None:
    settings = get_settings()
    settings.validate_runtime_safety()
    if not settings.database.worker_url:
        raise RuntimeError("WORKER_DATABASE_URL is required")
    worker_database_url = make_url(normalize_database_url(settings.database.worker_url))
    web_database_url = make_url(normalize_database_url(settings.database.url))
    if worker_database_url.username == web_database_url.username:
        raise RuntimeError("Worker and web runtime database roles must use distinct URLs")
    status = await get_schema_status_async(
        settings.database.worker_url,
        expected_embedding_dimension=settings.embedding.dimension,
    )
    if not status.compatible:
        raise RuntimeError("Worker database schema is incompatible")
    await validate_worker_database_role()
    factory = get_worker_session_factory()
    storage = LocalVersionedObjectStorage(settings.gateway.storage_dir)
    parser = BoundedDocumentParser(
        timeout_seconds=settings.gateway.parser_timeout_seconds,
        memory_limit_bytes=settings.gateway.parser_memory_limit_bytes,
        cpu_seconds=settings.gateway.parser_cpu_seconds,
        max_pages=settings.gateway.parser_max_pages,
        max_output_characters=settings.gateway.parser_max_output_characters,
    )
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    ingestion = DocumentIngestionWorker(
        factory,
        storage,
        parser,
        HTTPEmbeddingClient(),
        worker_id=worker_id,
        lease_seconds=settings.gateway.worker_lease_seconds,
        heartbeat_interval_seconds=settings.gateway.worker_heartbeat_seconds,
        chunk_size=settings.gateway.ingestion_chunk_size,
        chunk_overlap=settings.gateway.ingestion_chunk_overlap,
        max_chunks=settings.gateway.ingestion_max_chunks,
    )
    outbox = JobOutboxDispatcher(
        factory,
        _publish_internal_event,
        worker_id=f"{worker_id}:outbox",
        lease_seconds=settings.gateway.worker_lease_seconds,
    )
    maintenance = StorageMaintenanceRunner(
        factory,
        StorageConsistencyService(factory, storage),
        interval_seconds=settings.gateway.storage_reconcile_interval_seconds,
        staging_ttl_seconds=settings.gateway.storage_staging_ttl_seconds,
        orphan_grace_seconds=settings.gateway.storage_orphan_grace_seconds,
    )
    retention = RetentionMaintenanceRunner(
        RetentionService(factory, storage),
        interval_seconds=settings.gateway.retention_interval_seconds,
        archive_days=settings.gateway.retention_archive_days,
        revision_days=settings.gateway.retention_revision_days,
        operational_days=settings.gateway.retention_operational_days,
        batch_size=settings.gateway.retention_batch_size,
        max_batches_per_cycle=settings.gateway.retention_max_batches_per_cycle,
    )
    stopping = stop_event or asyncio.Event()
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(
            ingestion.run_until_stopped(
                stopping,
                idle_delay_seconds=settings.gateway.worker_idle_delay_seconds,
            )
        )
        tasks.create_task(
            outbox.run_until_stopped(
                stopping,
                idle_delay_seconds=settings.gateway.worker_idle_delay_seconds,
            )
        )
        tasks.create_task(maintenance.run_until_stopped(stopping))
        if settings.gateway.retention_purge_enabled:
            tasks.create_task(retention.run_until_stopped(stopping))


async def _main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def stop() -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, stop)
        except (NotImplementedError, RuntimeError):
            signal.signal(signal_name, lambda *_: stop())
    try:
        await run_worker(stop_event)
    finally:
        await HTTPEmbeddingClient.close_shared_client()
        await close_db_engine()


def main() -> None:
    configure_logging(get_settings().gateway.log_level)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
