from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import logging
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.gateway.application.ports.object_storage import IVersionedObjectStorage
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionChunkModel, DocumentRevisionModel, IngestionJobModel, JobOutboxModel, OperationalAlertModel, RetrievalUnitModel


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StorageConsistencyResult:
    deleted_storage_keys: tuple[str, ...]
    quarantined_revision_ids: tuple[UUID, ...]
    alert_codes: tuple[str, ...]


class StorageConsistencyService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        storage: IVersionedObjectStorage,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage

    async def reconcile(
        self,
        *,
        now: datetime,
        staging_ttl: timedelta,
        orphan_grace: timedelta,
    ) -> StorageConsistencyResult:
        if staging_ttl.total_seconds() <= 0 or orphan_grace.total_seconds() <= 0:
            raise ValueError("Storage reconciliation durations must be positive")
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        DocumentRevisionModel.id,
                        DocumentRevisionModel.storage_key,
                        DocumentRevisionModel.staging_storage_key,
                        DocumentRevisionModel.size_bytes,
                        DocumentRevisionModel.checksum_sha256,
                    )
                )
            ).all()
        storage_references = {row.storage_key: row for row in rows if row.storage_key}
        staging_references = {row.staging_storage_key: row for row in rows if row.staging_storage_key}
        deleted = []
        for item in await self._storage.list_objects("staging"):
            if item.modified_at <= now - staging_ttl:
                if await self._storage.delete(item.storage_key):
                    deleted.append(item.storage_key)
                referenced = staging_references.get(item.storage_key)
                if referenced is not None:
                    await self._fail_expired_staging(referenced.id, now)
        for item in await self._storage.list_objects("objects"):
            if item.storage_key not in storage_references and item.modified_at <= now - orphan_grace:
                if await self._storage.delete(item.storage_key):
                    deleted.append(item.storage_key)

        failures = []
        for key, row in storage_references.items():
            if not await self._storage.exists(key):
                failures.append((row.id, "storage_object_missing"))
                continue
            content = await self._storage.read(key)
            if len(content) != row.size_bytes:
                failures.append((row.id, "storage_size_mismatch"))
                continue
            if hashlib.sha256(content).hexdigest() != row.checksum_sha256:
                failures.append((row.id, "storage_checksum_mismatch"))
        quarantined = []
        alert_codes = []
        for revision_id, code in failures:
            if await self._quarantine(revision_id, code, now):
                quarantined.append(revision_id)
                alert_codes.append(code)
                logger.error(
                    "Referenced storage object failed integrity verification",
                    extra={"failure_code": code, "document_revision_id": str(revision_id)},
                )
        return StorageConsistencyResult(
            deleted_storage_keys=tuple(sorted(deleted)),
            quarantined_revision_ids=tuple(quarantined),
            alert_codes=tuple(alert_codes),
        )

    async def _fail_expired_staging(self, revision_id: UUID, now: datetime) -> None:
        async with self._session_factory.begin() as session:
            await session.execute(
                update(DocumentRevisionModel)
                .where(
                    DocumentRevisionModel.id == revision_id,
                    DocumentRevisionModel.storage_key.is_(None),
                    DocumentRevisionModel.status.in_(("pending", "processing")),
                )
                .values(
                    staging_storage_key=None,
                    status="failed",
                    failure_code="staging_expired",
                )
            )
            await session.execute(
                update(IngestionJobModel)
                .where(
                    IngestionJobModel.document_revision_id == revision_id,
                    IngestionJobModel.state.not_in(("succeeded", "failed", "cancelled")),
                )
                .values(
                    state="failed",
                    last_error_code="staging_expired",
                    finished_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                    claim_token=None,
                    updated_at=now,
                )
            )
            await self._emit_alert(
                session,
                revision_id,
                "staging_expired",
                {"document_revision_id": str(revision_id)},
            )

    async def _quarantine(self, revision_id: UUID, code: str, now: datetime) -> bool:
        async with self._session_factory.begin() as session:
            revision = await session.scalar(
                select(DocumentRevisionModel)
                .where(DocumentRevisionModel.id == revision_id)
                .with_for_update()
            )
            if revision is None:
                return False
            changed = revision.status != "quarantined" or revision.failure_code != code
            revision.status = "quarantined"
            revision.failure_code = code
            chunk_ids = select(DocumentRevisionChunkModel.id).where(
                DocumentRevisionChunkModel.document_revision_id == revision_id
            )
            await session.execute(
                update(RetrievalUnitModel)
                .where(
                    RetrievalUnitModel.document_revision_chunk_id.in_(chunk_ids),
                    RetrievalUnitModel.active.is_(True),
                )
                .values(active=False, deactivated_at=now)
            )
            await session.execute(
                update(DocumentModel)
                .where(
                    DocumentModel.id == revision.document_id,
                    DocumentModel.current_revision_id == revision_id,
                )
                .values(
                    current_revision_id=None,
                    revision=DocumentModel.revision + 1,
                    updated_at=now,
                )
            )
            job = await session.scalar(
                select(IngestionJobModel).where(
                    IngestionJobModel.document_revision_id == revision_id
                )
            )
            if job is not None and job.state not in {"succeeded", "failed", "cancelled"}:
                job.state = "failed"
                job.last_error_code = code
                job.finished_at = now
                job.lease_owner = None
                job.lease_expires_at = None
                job.claim_token = None
                job.updated_at = now
            if job is not None:
                key = f"storage:{revision_id}:{code}"
                exists = await session.scalar(
                    select(func.count()).select_from(JobOutboxModel).where(
                        JobOutboxModel.deduplication_key == key
                    )
                )
                if not exists:
                    session.add(
                        JobOutboxModel(
                            job_id=job.id,
                            event_type="storage.integrity_failed",
                            deduplication_key=key,
                            payload={
                                "document_revision_id": str(revision_id),
                                "failure_code": code,
                            },
                        )
                    )
            await self._emit_alert(
                session,
                revision_id,
                code,
                {
                    "document_revision_id": str(revision_id),
                    "storage_key": revision.storage_key,
                },
            )
            await session.flush()
            return changed

    async def _emit_alert(
        self,
        session,
        revision_id: UUID,
        code: str,
        details: dict,
    ) -> None:
        exists = await session.scalar(
            select(func.count()).select_from(OperationalAlertModel).where(
                OperationalAlertModel.code == code,
                OperationalAlertModel.resource_type == "document_revision",
                OperationalAlertModel.resource_id == str(revision_id),
                OperationalAlertModel.acknowledged_at.is_(None),
            )
        )
        if not exists:
            session.add(
                OperationalAlertModel(
                    severity="critical" if code.startswith("storage_") else "error",
                    code=code,
                    resource_type="document_revision",
                    resource_id=str(revision_id),
                    details=details,
                )
            )
