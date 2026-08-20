from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.ingestion_job_service import IngestionJobService, JobClaim
from src.gateway.domain.exceptions import AuthorizationException, ConcurrencyConflictException, JobLeaseLostException, ValidationException
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionChunkModel, DocumentRevisionModel, EmbeddingGenerationModel, IngestionJobModel, JobOutboxModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import Workspace


@dataclass(frozen=True, slots=True)
class ActivationChunk:
    content: str
    content_hash: str
    embedding: list[float]
    language: str | None = None
    parser_metadata: dict = field(default_factory=dict)


class DocumentActivationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def activate(
        self,
        claim: JobClaim,
        *,
        parser_version: str,
        embedding_generation_id: UUID,
        chunks: list[ActivationChunk],
    ) -> None:
        now = await self._session.scalar(select(func.now()))
        job = await self._session.scalar(
            select(IngestionJobModel)
            .where(
                IngestionJobModel.id == claim.job_id,
                IngestionJobModel.state == "running",
                IngestionJobModel.claim_token == claim.claim_token,
                IngestionJobModel.lease_expires_at > now,
                IngestionJobModel.cancellation_requested.is_(False),
            )
            .with_for_update()
        )
        if job is None:
            raise JobLeaseLostException()
        if (
            job.document_id != claim.document_id
            or job.document_revision_id != claim.document_revision_id
            or job.space_id != claim.space_id
        ):
            raise JobLeaseLostException()
        authorized = await self._session.scalar(
            select(func.count())
            .select_from(SpaceMembershipModel)
            .join(MemberModel, MemberModel.id == SpaceMembershipModel.member_id)
            .join(Workspace, Workspace.id == SpaceMembershipModel.space_id)
            .where(
                SpaceMembershipModel.space_id == job.space_id,
                SpaceMembershipModel.member_id == job.initiated_by_member_id,
                SpaceMembershipModel.role.in_(("owner", "editor")),
                MemberModel.status == "active",
                Workspace.archived_at.is_(None),
            )
        )
        if not authorized:
            raise AuthorizationException()
        generation = await self._session.get(EmbeddingGenerationModel, embedding_generation_id)
        if generation is None or generation.status != "active" or generation.purpose != "retrieval":
            raise ValidationException("The retrieval embedding generation is unavailable.")
        self._validate_chunks(chunks, generation.dimensions)
        document = await self._session.scalar(
            select(DocumentModel).where(DocumentModel.id == claim.document_id).with_for_update()
        )
        revision = await self._session.scalar(
            select(DocumentRevisionModel)
            .where(DocumentRevisionModel.id == claim.document_revision_id)
            .with_for_update()
        )
        if (
            document is None
            or revision is None
            or document.space_id != claim.space_id
            or revision.document_id != document.id
            or revision.space_id != document.space_id
            or revision.storage_key is None
            or revision.status not in {"pending", "processing", "ready"}
        ):
            raise ConcurrencyConflictException("Document activation state is inconsistent.")
        existing_chunks = await self._session.scalar(
            select(func.count())
            .select_from(DocumentRevisionChunkModel)
            .where(DocumentRevisionChunkModel.document_revision_id == revision.id)
        )
        if existing_chunks:
            raise ConcurrencyConflictException("Document revision artifacts already exist.")

        old_revision_id = document.current_revision_id
        if old_revision_id is not None:
            old_chunk_ids = select(DocumentRevisionChunkModel.id).where(
                DocumentRevisionChunkModel.document_revision_id == old_revision_id
            )
            await self._session.execute(
                update(RetrievalUnitModel)
                .where(
                    RetrievalUnitModel.document_revision_chunk_id.in_(old_chunk_ids),
                    RetrievalUnitModel.active.is_(True),
                )
                .values(active=False, deactivated_at=now)
            )
            await self._session.execute(
                update(DocumentRevisionModel)
                .where(
                    DocumentRevisionModel.id == old_revision_id,
                    DocumentRevisionModel.status == "active",
                )
                .values(status="ready")
            )

        derived = [(uuid4(), index, artifact) for index, artifact in enumerate(chunks)]
        for chunk_id, index, artifact in derived:
            self._session.add(
                DocumentRevisionChunkModel(
                    id=chunk_id,
                    document_revision_id=revision.id,
                    document_id=document.id,
                    space_id=document.space_id,
                    chunk_index=index,
                    content=artifact.content,
                    content_hash=artifact.content_hash,
                    language=artifact.language,
                    parser_metadata=artifact.parser_metadata,
                )
            )
        await self._session.flush()
        for chunk_id, index, artifact in derived:
            self._session.add(
                RetrievalUnitModel(
                    space_id=document.space_id,
                    source_type="document_chunk",
                    document_revision_chunk_id=chunk_id,
                    embedding_generation_id=embedding_generation_id,
                    title=document.display_name,
                    content=artifact.content,
                    language=artifact.language,
                    source_metadata={
                        "document_id": str(document.id),
                        "document_revision_id": str(revision.id),
                        "chunk_index": index,
                        "source_filename": revision.original_filename,
                        "parser_version": parser_version,
                    },
                    embedding=artifact.embedding,
                    active=True,
                )
            )

        revision.parser_version = parser_version
        revision.status = "active"
        revision.ready_at = now
        revision.activated_at = now
        document.current_revision_id = revision.id
        document.revision += 1
        document.updated_at = now
        await self._session.flush()
        await IngestionJobService(self._session).complete(job.id, claim.claim_token)
        self._session.add(
            JobOutboxModel(
                job_id=job.id,
                event_type="ingestion.succeeded",
                deduplication_key=f"ingestion:{job.id}:succeeded",
                payload={
                    "job_id": str(job.id),
                    "document_id": str(document.id),
                    "document_revision_id": str(revision.id),
                },
            )
        )

    def _validate_chunks(self, chunks: list[ActivationChunk], dimensions: int) -> None:
        for chunk in chunks:
            if not chunk.content or len(chunk.content_hash) != 64:
                raise ValidationException("A derived document chunk is invalid.")
            if len(chunk.embedding) != dimensions:
                raise ValidationException("A derived document embedding has the wrong dimensions.")
