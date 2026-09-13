from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.ingestion_job_service import JobClaim
from src.gateway.domain.entities import KnowledgeItem as DomainKnowledgeItem
from src.gateway.domain.exceptions import AuthorizationException, ItemNotFoundException, JobLeaseLostException
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, IngestionJobModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import KnowledgeItem, KnowledgeRevision

logger = logging.getLogger(__name__)

ENRICHMENT_JOB_TYPE = "knowledge_enrichment"


async def queue_enrichment_job(
    session: AsyncSession,
    *,
    principal: Principal,
    space_id: str,
    item_id: UUID,
    revision_id: UUID,
    idempotency_key: str,
) -> UUID:
    try:
        member_id = UUID(principal.subject_id)
    except ValueError as exc:
        raise AuthorizationException() from exc
    job_id = uuid4()
    session.add(
        IngestionJobModel(
            id=job_id,
            space_id=space_id,
            document_id=None,
            document_revision_id=None,
            knowledge_item_id=item_id,
            knowledge_revision_id=revision_id,
            initiated_by_member_id=member_id,
            job_type=ENRICHMENT_JOB_TYPE,
            state="queued",
            idempotency_key=f"knowledge-enrichment:{idempotency_key.strip()}",
        )
    )
    await session.flush()
    return job_id


async def enrichment_status(session: AsyncSession, revision_id: UUID) -> str | None:
    job = await session.scalar(
        select(IngestionJobModel)
        .where(
            IngestionJobModel.job_type == ENRICHMENT_JOB_TYPE,
            IngestionJobModel.knowledge_revision_id == revision_id,
        )
        .order_by(IngestionJobModel.created_at.desc(), IngestionJobModel.id.desc())
        .limit(1)
    )
    if job is None:
        return None
    if job.state == "succeeded":
        return "enriched"
    if job.state in {"failed", "cancelled"}:
        return "failed"
    return "pending"


async def enrich_claim(session: AsyncSession, claim: JobClaim, embedding_client) -> None:
    now = datetime.now(timezone.utc)
    job = await session.scalar(
        select(IngestionJobModel)
        .where(
            IngestionJobModel.id == claim.job_id,
            IngestionJobModel.job_type == ENRICHMENT_JOB_TYPE,
            IngestionJobModel.state == "running",
            IngestionJobModel.claim_token == claim.claim_token,
            IngestionJobModel.lease_expires_at > now,
            IngestionJobModel.cancellation_requested.is_(False),
        )
        .with_for_update()
    )
    if job is None:
        raise JobLeaseLostException()
    item = await session.get(KnowledgeItem, job.knowledge_item_id)
    revision = await session.get(KnowledgeRevision, job.knowledge_revision_id)
    if item is None or revision is None or revision.item_id != item.id:
        raise ItemNotFoundException()
    generation = await session.scalar(
        select(EmbeddingGenerationModel).where(
            EmbeddingGenerationModel.purpose == "retrieval",
            EmbeddingGenerationModel.status == "active",
        )
    )
    if generation is None:
        raise ItemNotFoundException()
    text = f"{revision.title or ''}\n\n{revision.content}".strip()
    embedding: list[float] | None = None
    if text:
        try:
            embeddings = await embedding_client.embed_texts([text])
        except Exception as exc:
            logger.warning(
                "Knowledge enrichment embedding failed",
                extra={"exception_class": type(exc).__name__, "knowledge_item_id": str(item.id)},
            )
            raise
        embedding = embeddings[0] if embeddings else None
    if embedding is not None and len(embedding) != generation.dimensions:
        embedding = None
    revision.embedding = embedding
    await session.execute(
        RetrievalUnitModel.__table__.update()
        .where(
            RetrievalUnitModel.knowledge_revision_id == revision.id,
            RetrievalUnitModel.active.is_(True),
        )
        .values(active=False, deactivated_at=now)
    )
    session.add(
        RetrievalUnitModel(
            space_id=item.workspace_id,
            source_type="knowledge_revision",
            knowledge_revision_id=revision.id,
            embedding_generation_id=generation.id,
            title=revision.title or item.title,
            content=revision.content,
            language=None,
            source_metadata={
                "knowledge_item_id": str(item.id),
                "knowledge_revision_id": str(revision.id),
                "version": revision.version,
                "tags": list(revision.tags),
            },
            embedding=embedding,
            active=True,
        )
    )
    await session.flush()


def enrichment_receipt(*, job_id: UUID, state: str, item: DomainKnowledgeItem) -> dict:
    revision = item.current_revision
    return {
        "job_id": str(job_id),
        "state": state,
        "item_id": str(item.id),
        "revision_id": str(revision.id) if revision is not None else None,
    }
