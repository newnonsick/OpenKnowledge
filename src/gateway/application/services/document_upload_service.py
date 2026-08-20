from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.gateway.application.ports.object_storage import IVersionedObjectStorage
from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.domain.authorization import Action
from src.gateway.domain.exceptions import AuthorizationException, ConcurrencyConflictException, ValidationException
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionModel, IngestionJobModel, JobOutboxModel
from src.gateway.infrastructure.persistence.principal_context import bind_principal, reset_principal


@dataclass(frozen=True, slots=True)
class UploadReceipt:
    document_id: UUID
    revision_id: UUID
    job_id: UUID
    job_state: str
    duplicate_candidate_revision_id: UUID | None = None
    request_fingerprint: str | None = None


class DocumentUploadService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        storage: IVersionedObjectStorage,
        *,
        max_upload_bytes: int,
    ) -> None:
        if max_upload_bytes <= 0:
            raise ValueError("A positive upload limit is required")
        self._session_factory = session_factory
        self._storage = storage
        self._max_upload_bytes = max_upload_bytes

    async def upload_new(
        self,
        *,
        principal: Principal,
        space_id: str,
        display_name: str,
        original_filename: str,
        mime_type: str,
        chunks,
        idempotency_key: str,
    ) -> UploadReceipt:
        self._validate(principal, display_name, original_filename, mime_type, idempotency_key)
        upload_id = uuid4()
        staged = await self._storage.stage(
            space_id=space_id,
            upload_id=upload_id,
            chunks=chunks,
            max_bytes=self._max_upload_bytes,
        )
        request_fingerprint = self._fingerprint(
            operation="document.create",
            document_id=None,
            display_name=display_name,
            original_filename=original_filename,
            mime_type=mime_type,
            size_bytes=staged.size_bytes,
            checksum_sha256=staged.checksum_sha256,
        )
        existing = await self._find_existing(principal, space_id, idempotency_key)
        if existing is not None:
            await self._storage.delete(staged.storage_key)
            self._require_matching_fingerprint(existing, request_fingerprint)
            return await self._resume_if_preparing(principal, existing)
        document_id = uuid4()
        revision_id = uuid4()
        job_id = uuid4()
        duplicate_id = None
        try:
            token = bind_principal(principal)
            try:
                async with self._session_factory.begin() as session:
                    await AuthorizationService(session).authorize_space(
                        principal,
                        space_id,
                        Action.CONTENT_WRITE,
                    )
                    duplicate_id = await session.scalar(
                        select(DocumentRevisionModel.id)
                        .where(
                            DocumentRevisionModel.space_id == space_id,
                            DocumentRevisionModel.checksum_sha256 == staged.checksum_sha256,
                            DocumentRevisionModel.status.not_in(("failed", "quarantined", "cancelled")),
                        )
                        .order_by(DocumentRevisionModel.created_at, DocumentRevisionModel.id)
                        .limit(1)
                    )
                    session.add(
                        DocumentModel(
                            id=document_id,
                            space_id=space_id,
                            display_name=display_name.strip(),
                            created_by_member_id=UUID(principal.subject_id),
                        )
                    )
                    await session.flush()
                    session.add(
                        DocumentRevisionModel(
                            id=revision_id,
                            document_id=document_id,
                            space_id=space_id,
                            version=1,
                            original_filename=original_filename.strip(),
                            mime_type=mime_type.strip().lower(),
                            size_bytes=staged.size_bytes,
                            checksum_sha256=staged.checksum_sha256,
                            staging_storage_key=staged.storage_key,
                            status="pending",
                            created_by_member_id=UUID(principal.subject_id),
                        )
                    )
                    await session.flush()
                    session.add(
                        IngestionJobModel(
                            id=job_id,
                            space_id=space_id,
                            document_id=document_id,
                            document_revision_id=revision_id,
                            initiated_by_member_id=UUID(principal.subject_id),
                            state="preparing",
                            idempotency_key=idempotency_key.strip(),
                            request_fingerprint=request_fingerprint,
                        )
                    )
            finally:
                reset_principal(token)
        except IntegrityError:
            await self._storage.delete(staged.storage_key)
            existing = await self._find_existing(principal, space_id, idempotency_key)
            if existing is None:
                raise ConcurrencyConflictException("The idempotency key is already in use.")
            self._require_matching_fingerprint(existing, request_fingerprint)
            return await self._resume_if_preparing(principal, existing)
        except Exception:
            await self._storage.delete(staged.storage_key)
            raise

        receipt = UploadReceipt(
            document_id=document_id,
            revision_id=revision_id,
            job_id=job_id,
            job_state="preparing",
            duplicate_candidate_revision_id=duplicate_id,
            request_fingerprint=request_fingerprint,
        )
        return await self._finalize(principal, receipt, staged.storage_key)

    async def upload_revision(
        self,
        *,
        principal: Principal,
        document_id: UUID,
        expected_revision: int,
        original_filename: str,
        mime_type: str,
        chunks,
        idempotency_key: str,
    ) -> UploadReceipt:
        self._validate(
            principal,
            original_filename,
            original_filename,
            mime_type,
            idempotency_key,
        )
        if expected_revision <= 0:
            raise ValidationException("A positive expected document revision is required.")
        space_id = await self._document_space(principal, document_id)
        staged = await self._storage.stage(
            space_id=space_id,
            upload_id=uuid4(),
            chunks=chunks,
            max_bytes=self._max_upload_bytes,
        )
        request_fingerprint = self._fingerprint(
            operation="document.revise",
            document_id=document_id,
            display_name=None,
            original_filename=original_filename,
            mime_type=mime_type,
            size_bytes=staged.size_bytes,
            checksum_sha256=staged.checksum_sha256,
        )
        existing = await self._find_existing(principal, space_id, idempotency_key)
        if existing is not None:
            await self._storage.delete(staged.storage_key)
            if existing.document_id != document_id:
                raise ConcurrencyConflictException("The idempotency key belongs to another document.")
            self._require_matching_fingerprint(existing, request_fingerprint)
            return await self._resume_if_preparing(principal, existing)
        revision_id = uuid4()
        job_id = uuid4()
        duplicate_id = None
        try:
            token = bind_principal(principal)
            try:
                async with self._session_factory.begin() as session:
                    await AuthorizationService(session).authorize_space(
                        principal,
                        space_id,
                        Action.CONTENT_WRITE,
                    )
                    document = await session.scalar(
                        select(DocumentModel)
                        .where(
                            DocumentModel.id == document_id,
                            DocumentModel.space_id == space_id,
                            DocumentModel.archived_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if document is None or document.current_revision_id is None:
                        raise ConcurrencyConflictException("The document has no active revision.")
                    if document.revision != expected_revision:
                        raise ConcurrencyConflictException(
                            message_or_item_id=str(document_id),
                            expected_version=expected_revision,
                            actual_version=document.revision,
                        )
                    current = await session.get(
                        DocumentRevisionModel,
                        document.current_revision_id,
                    )
                    if current is None or current.status != "active":
                        raise ConcurrencyConflictException("The active document revision is inconsistent.")
                    duplicate_id = await session.scalar(
                        select(DocumentRevisionModel.id)
                        .where(
                            DocumentRevisionModel.space_id == space_id,
                            DocumentRevisionModel.checksum_sha256 == staged.checksum_sha256,
                            DocumentRevisionModel.status.not_in(("failed", "quarantined", "cancelled")),
                        )
                        .order_by(DocumentRevisionModel.created_at, DocumentRevisionModel.id)
                        .limit(1)
                    )
                    next_version = current.version + 1
                    session.add(
                        DocumentRevisionModel(
                            id=revision_id,
                            document_id=document_id,
                            space_id=space_id,
                            version=next_version,
                            original_filename=original_filename.strip(),
                            mime_type=mime_type.strip().lower(),
                            size_bytes=staged.size_bytes,
                            checksum_sha256=staged.checksum_sha256,
                            staging_storage_key=staged.storage_key,
                            status="pending",
                            created_by_member_id=UUID(principal.subject_id),
                        )
                    )
                    await session.flush()
                    session.add(
                        IngestionJobModel(
                            id=job_id,
                            space_id=space_id,
                            document_id=document_id,
                            document_revision_id=revision_id,
                            initiated_by_member_id=UUID(principal.subject_id),
                            state="preparing",
                            idempotency_key=idempotency_key.strip(),
                            request_fingerprint=request_fingerprint,
                        )
                    )
                    document.revision += 1
                    await session.flush()
            finally:
                reset_principal(token)
        except IntegrityError:
            await self._storage.delete(staged.storage_key)
            existing = await self._find_existing(principal, space_id, idempotency_key)
            if existing is None or existing.document_id != document_id:
                raise ConcurrencyConflictException("A concurrent document revision already exists.")
            self._require_matching_fingerprint(existing, request_fingerprint)
            return await self._resume_if_preparing(principal, existing)
        except Exception:
            await self._storage.delete(staged.storage_key)
            raise

        receipt = UploadReceipt(
            document_id=document_id,
            revision_id=revision_id,
            job_id=job_id,
            job_state="preparing",
            duplicate_candidate_revision_id=duplicate_id,
            request_fingerprint=request_fingerprint,
        )
        return await self._finalize(principal, receipt, staged.storage_key)

    async def _document_space(self, principal: Principal, document_id: UUID) -> str:
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                space_id = await session.scalar(
                    select(DocumentModel.space_id).where(
                        DocumentModel.id == document_id,
                        DocumentModel.archived_at.is_(None),
                    )
                )
                if space_id is None:
                    raise AuthorizationException()
                await AuthorizationService(session).authorize_space(
                    principal,
                    space_id,
                    Action.CONTENT_WRITE,
                )
                return space_id
        finally:
            reset_principal(token)

    async def _find_existing(
        self,
        principal: Principal,
        space_id: str,
        idempotency_key: str,
    ) -> UploadReceipt | None:
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                await AuthorizationService(session).authorize_space(
                    principal,
                    space_id,
                    Action.CONTENT_WRITE,
                )
                row = (
                    await session.execute(
                        select(
                            IngestionJobModel.document_id,
                            IngestionJobModel.document_revision_id,
                            IngestionJobModel.id,
                            IngestionJobModel.state,
                            IngestionJobModel.request_fingerprint,
                        ).where(
                            IngestionJobModel.initiated_by_member_id == UUID(principal.subject_id),
                            IngestionJobModel.space_id == space_id,
                            IngestionJobModel.idempotency_key == idempotency_key.strip(),
                        )
                    )
                ).one_or_none()
                if row is None:
                    return None
                return UploadReceipt(
                    document_id=row.document_id,
                    revision_id=row.document_revision_id,
                    job_id=row.id,
                    job_state=row.state,
                    request_fingerprint=row.request_fingerprint,
                )
        finally:
            reset_principal(token)

    async def _resume_if_preparing(
        self,
        principal: Principal,
        receipt: UploadReceipt,
    ) -> UploadReceipt:
        if receipt.job_state != "preparing":
            return receipt
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                revision = await session.get(DocumentRevisionModel, receipt.revision_id)
                if revision is None or revision.staging_storage_key is None:
                    raise ConcurrencyConflictException("Upload preparation state is inconsistent.")
                staging_key = revision.staging_storage_key
        finally:
            reset_principal(token)
        return await self._finalize(principal, receipt, staging_key)

    async def _finalize(
        self,
        principal: Principal,
        receipt: UploadReceipt,
        staging_key: str,
    ) -> UploadReceipt:
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                revision = await session.get(DocumentRevisionModel, receipt.revision_id)
                if revision is None:
                    raise ConcurrencyConflictException("Upload revision is unavailable.")
                space_id = revision.space_id
        finally:
            reset_principal(token)
        final_key = await self._storage.finalize(
            staging_key,
            space_id=space_id,
            document_id=receipt.document_id,
            revision_id=receipt.revision_id,
        )
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                revision_result = await session.execute(
                    update(DocumentRevisionModel)
                    .where(
                        DocumentRevisionModel.id == receipt.revision_id,
                        DocumentRevisionModel.status == "pending",
                        DocumentRevisionModel.staging_storage_key == staging_key,
                    )
                    .values(storage_key=final_key, staging_storage_key=None)
                    .returning(DocumentRevisionModel.id)
                )
                job_result = await session.execute(
                    update(IngestionJobModel)
                    .where(
                        IngestionJobModel.id == receipt.job_id,
                        IngestionJobModel.state == "preparing",
                    )
                    .values(state="queued")
                    .returning(IngestionJobModel.id)
                )
                revision_result.scalar_one_or_none()
                job_result.scalar_one_or_none()
                revision = await session.get(DocumentRevisionModel, receipt.revision_id)
                job = await session.get(IngestionJobModel, receipt.job_id)
                if (
                    revision is None
                    or job is None
                    or revision.storage_key != final_key
                    or revision.staging_storage_key is not None
                    or job.document_revision_id != revision.id
                    or job.state == "preparing"
                ):
                    raise ConcurrencyConflictException("Upload activation state changed unexpectedly.")
                deduplication_key = f"ingestion:{receipt.job_id}:queued"
                outbox_exists = await session.scalar(
                    select(JobOutboxModel.id).where(
                        JobOutboxModel.deduplication_key == deduplication_key
                    )
                )
                if outbox_exists is None:
                    session.add(
                        JobOutboxModel(
                            job_id=receipt.job_id,
                            event_type="ingestion.queued",
                            deduplication_key=deduplication_key,
                            payload={
                                "job_id": str(receipt.job_id),
                                "document_revision_id": str(receipt.revision_id),
                            },
                        )
                    )
                current_job_state = job.state
        finally:
            reset_principal(token)
        return UploadReceipt(
            document_id=receipt.document_id,
            revision_id=receipt.revision_id,
            job_id=receipt.job_id,
            job_state=current_job_state,
            duplicate_candidate_revision_id=receipt.duplicate_candidate_revision_id,
            request_fingerprint=receipt.request_fingerprint,
        )

    @staticmethod
    def _fingerprint(
        *,
        operation: str,
        document_id: UUID | None,
        display_name: str | None,
        original_filename: str,
        mime_type: str,
        size_bytes: int,
        checksum_sha256: str,
    ) -> str:
        payload = {
            "operation": operation,
            "document_id": str(document_id) if document_id else None,
            "display_name": display_name.strip() if display_name is not None else None,
            "original_filename": original_filename.strip(),
            "mime_type": mime_type.strip().lower(),
            "size_bytes": size_bytes,
            "checksum_sha256": checksum_sha256,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _require_matching_fingerprint(receipt: UploadReceipt, expected: str) -> None:
        if receipt.request_fingerprint != expected:
            raise ConcurrencyConflictException(
                "The idempotency key was already used for a different request."
            )

    def _validate(
        self,
        principal: Principal,
        display_name: str,
        original_filename: str,
        mime_type: str,
        idempotency_key: str,
    ) -> None:
        if not principal.active or principal.restricted:
            raise AuthorizationException()
        if "*" not in principal.scopes and "knowledge:write" not in principal.scopes:
            raise AuthorizationException()
        if not display_name.strip() or len(display_name) > 500:
            raise ValidationException("A valid display name is required.")
        if not original_filename.strip() or len(original_filename) > 500:
            raise ValidationException("A valid original filename is required.")
        if not mime_type.strip() or len(mime_type) > 255:
            raise ValidationException("A valid MIME type is required.")
        if not idempotency_key.strip() or len(idempotency_key) > 255:
            raise ValidationException("A valid idempotency key is required.")
        try:
            UUID(principal.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
