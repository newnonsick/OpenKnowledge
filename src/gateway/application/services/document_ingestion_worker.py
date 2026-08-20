from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
from time import perf_counter

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.gateway.application.parsers.chunker import Chunker
from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.application.ports.object_storage import IVersionedObjectStorage
from src.gateway.application.services.document_activation_service import ActivationChunk, DocumentActivationService
from src.gateway.application.services.ingestion_job_service import IngestionJobService, JobClaim
from src.gateway.domain.exceptions import AuthorizationException, ConcurrencyConflictException, EmbeddingException, ItemNotFoundException, JobLeaseLostException, ParserTimeoutException, StorageException, ValidationException
from src.gateway.infrastructure.persistence.ingestion_models import DocumentRevisionModel, EmbeddingGenerationModel, IngestionJobModel
from src.gateway.observability import increment_metric, observe_metric, set_metric_gauge


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _IngestionFailure(Exception):
    code: str
    retryable: bool
    quarantined: bool = False


class DocumentIngestionWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        storage: IVersionedObjectStorage,
        parser,
        embedding_client: IEmbeddingClient,
        *,
        worker_id: str,
        lease_seconds: int,
        heartbeat_interval_seconds: float,
        chunk_size: int,
        chunk_overlap: int,
        max_chunks: int,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("A worker id is required")
        if lease_seconds <= 0 or heartbeat_interval_seconds <= 0:
            raise ValueError("Worker lease settings must be positive")
        if heartbeat_interval_seconds >= lease_seconds:
            raise ValueError("Worker heartbeat interval must be shorter than the lease")
        if chunk_size <= 0 or chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("Worker chunk settings are invalid")
        if max_chunks <= 0:
            raise ValueError("Worker chunk limit must be positive")
        self._session_factory = session_factory
        self._storage = storage
        self._parser = parser
        self._embedding_client = embedding_client
        self._worker_id = worker_id.strip()
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._chunker = Chunker(chunk_size, chunk_overlap)
        self._max_chunks = max_chunks
        self._last_queue_observation = 0.0

    async def run_once(self):
        async with self._session_factory.begin() as session:
            jobs = IngestionJobService(session)
            current_tick = perf_counter()
            if current_tick - self._last_queue_observation >= 15:
                for state, depth in (await jobs.queue_depths()).items():
                    set_metric_gauge("gateway_ingestion_queue_depth", depth, state=state)
                self._last_queue_observation = current_tick
            claim = await jobs.claim_next(
                self._worker_id,
                lease_seconds=self._lease_seconds,
            )
        if claim is None:
            return None
        queued_at = claim.queued_at
        if queued_at.tzinfo is None or queued_at.utcoffset() is None:
            queued_at = queued_at.replace(tzinfo=timezone.utc)
        observe_metric(
            "gateway_ingestion_claim_latency_seconds",
            max(0.0, (datetime.now(timezone.utc) - queued_at).total_seconds()),
            outcome="success",
        )
        increment_metric("gateway_ingestion_events_total", event="claim", outcome="success")
        processing = asyncio.create_task(self._process_claim_observed(claim))
        try:
            while True:
                done, _ = await asyncio.wait(
                    {processing},
                    timeout=self._heartbeat_interval_seconds,
                )
                if done:
                    await processing
                    return claim.job_id
                try:
                    async with self._session_factory.begin() as session:
                        await IngestionJobService(session).heartbeat(
                            claim.job_id,
                            claim.claim_token,
                            lease_seconds=self._lease_seconds,
                        )
                except JobLeaseLostException:
                    if processing.done():
                        await processing
                        return claim.job_id
                    processing.cancel()
                    with suppress(asyncio.CancelledError):
                        await processing
                    raise
        finally:
            if not processing.done():
                processing.cancel()
                with suppress(asyncio.CancelledError):
                    await processing

    async def _process_claim_observed(self, claim: JobClaim) -> str:
        started = perf_counter()
        outcome = "error"
        try:
            outcome = await self._process_claim(claim)
            return outcome
        except JobLeaseLostException:
            outcome = "lease_lost"
            raise
        finally:
            observe_metric("gateway_ingestion_processing_duration_seconds", perf_counter() - started, outcome=outcome)
            event = "terminal" if outcome in {"cancelled", "failed", "succeeded"} else "attempt"
            increment_metric("gateway_ingestion_events_total", event=event, outcome=outcome)
            logger.info(
                "Ingestion job attempt completed",
                extra={"attempt": claim.attempt_count, "job_id": claim.job_id, "outcome": outcome, "worker_id": self._worker_id},
            )

    async def run_until_stopped(
        self,
        stop_event: asyncio.Event,
        *,
        idle_delay_seconds: float = 0.5,
    ) -> None:
        if idle_delay_seconds <= 0:
            raise ValueError("Worker idle delay must be positive")
        while not stop_event.is_set():
            claimed = await self.run_once()
            if claimed is None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=idle_delay_seconds)
                except TimeoutError:
                    pass

    async def _process_claim(self, claim: JobClaim) -> str:
        try:
            revision = await self._mark_processing(claim)
            if not await self._checkpoint(claim, 5):
                return "cancelled"
            try:
                content = await self._storage.read(revision.storage_key)
            except ItemNotFoundException as exc:
                raise _IngestionFailure("storage_object_missing", False, True) from exc
            except StorageException as exc:
                raise _IngestionFailure("storage_read_failed", True) from exc
            if len(content) != revision.size_bytes:
                raise _IngestionFailure("storage_size_mismatch", False, True)
            if hashlib.sha256(content).hexdigest() != revision.checksum_sha256:
                raise _IngestionFailure("storage_checksum_mismatch", False, True)
            if not await self._checkpoint(claim, 20):
                return "cancelled"
            try:
                parsed = await self._parser.parse(
                    filename=revision.original_filename,
                    mime_type=revision.mime_type,
                    content=content,
                )
            except ParserTimeoutException as exc:
                raise _IngestionFailure("parser_timeout", False) from exc
            except ValidationException as exc:
                raise _IngestionFailure("parser_rejected", False) from exc
            chunks = self._chunker.chunk_semantic(parsed.text)
            if not chunks:
                raise _IngestionFailure("empty_document", False)
            if len(chunks) > self._max_chunks:
                raise _IngestionFailure("chunk_limit_exceeded", False)
            if not await self._checkpoint(claim, 45):
                return "cancelled"
            generation = await self._active_generation()
            try:
                embeddings = await self._embedding_client.embed_texts(chunks)
            except EmbeddingException as exc:
                raise _IngestionFailure("embedding_provider_error", True) from exc
            if len(embeddings) != len(chunks):
                raise _IngestionFailure("embedding_count_mismatch", True)
            if not await self._checkpoint(claim, 80):
                return "cancelled"
            artifacts = [
                ActivationChunk(
                    content=value,
                    content_hash=hashlib.sha256(value.encode("utf-8")).hexdigest(),
                    embedding=embedding,
                    parser_metadata={
                        "chunk_index": index,
                        "parser_version": parsed.parser_version,
                    },
                )
                for index, (value, embedding) in enumerate(zip(chunks, embeddings, strict=True))
            ]
            async with self._session_factory.begin() as session:
                await DocumentActivationService(session).activate(
                    claim,
                    parser_version=parsed.parser_version,
                    embedding_generation_id=generation.id,
                    chunks=artifacts,
                )
            return "succeeded"
        except JobLeaseLostException:
            raise
        except _IngestionFailure as failure:
            return await self._record_failure(claim, failure)
        except AuthorizationException:
            return await self._record_failure(
                claim,
                _IngestionFailure("authorization_revoked", False),
            )
        except ConcurrencyConflictException:
            return await self._record_failure(
                claim,
                _IngestionFailure("activation_conflict", False),
            )
        except Exception as exc:
            return await self._record_failure(
                claim,
                _IngestionFailure(f"internal_{type(exc).__name__.lower()}", True),
            )

    async def _mark_processing(self, claim: JobClaim) -> DocumentRevisionModel:
        async with self._session_factory.begin() as session:
            now_service = IngestionJobService(session)
            await now_service.set_progress(claim.job_id, claim.claim_token, 1)
            revision = await session.scalar(
                select(DocumentRevisionModel)
                .where(
                    DocumentRevisionModel.id == claim.document_revision_id,
                    DocumentRevisionModel.document_id == claim.document_id,
                    DocumentRevisionModel.space_id == claim.space_id,
                    DocumentRevisionModel.status.in_(("pending", "processing")),
                    DocumentRevisionModel.storage_key.is_not(None),
                )
                .with_for_update()
            )
            if revision is None:
                raise ConcurrencyConflictException("Document revision cannot be processed.")
            revision.status = "processing"
            revision.failure_code = None
            await session.flush()
            return revision

    async def _checkpoint(self, claim: JobClaim, progress: int) -> bool:
        async with self._session_factory.begin() as session:
            jobs = IngestionJobService(session)
            if await jobs.is_cancellation_requested(claim.job_id, claim.claim_token):
                await jobs.acknowledge_cancellation(claim.job_id, claim.claim_token)
                return False
            await jobs.set_progress(claim.job_id, claim.claim_token, progress)
            return True

    async def _active_generation(self) -> EmbeddingGenerationModel:
        async with self._session_factory() as session:
            generation = await session.scalar(
                select(EmbeddingGenerationModel).where(
                    EmbeddingGenerationModel.purpose == "retrieval",
                    EmbeddingGenerationModel.status == "active",
                )
            )
            if generation is None:
                raise _IngestionFailure("embedding_generation_unavailable", True)
            if generation.dimensions != self._embedding_client.dimension:
                raise _IngestionFailure("embedding_dimension_mismatch", False)
            return generation

    async def _record_failure(
        self,
        claim: JobClaim,
        failure: _IngestionFailure,
    ) -> str:
        async with self._session_factory.begin() as session:
            state = await IngestionJobService(session).fail(
                claim.job_id,
                claim.claim_token,
                error_code=failure.code,
                retryable=failure.retryable,
            )
            if state == "retry_wait":
                revision_state = "pending"
            elif state == "cancelled":
                revision_state = "cancelled"
            elif failure.quarantined:
                revision_state = "quarantined"
            else:
                revision_state = "failed"
            await session.execute(
                update(DocumentRevisionModel)
                .where(
                    DocumentRevisionModel.id == claim.document_revision_id,
                    DocumentRevisionModel.status.in_(("pending", "processing", "ready")),
                )
                .values(status=revision_state, failure_code=failure.code)
            )
            return state
