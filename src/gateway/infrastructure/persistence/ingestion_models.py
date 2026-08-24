from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, CheckConstraint, Computed, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.gateway.infrastructure.persistence.models import Base, EMBED_DIM


class DocumentModel(Base):
    __tablename__ = "documents"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    display_name: Mapped[str] = mapped_column(String(500), nullable=False)
    current_revision_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_by_member_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="SET NULL"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default=text("1"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_documents_revision"),
        UniqueConstraint("id", "space_id", name="uq_documents_id_space"),
        ForeignKeyConstraint(
            ["current_revision_id", "id", "space_id"],
            ["document_revisions.id", "document_revisions.document_id", "document_revisions.space_id"],
            name="fk_documents_current_revision_parent_space",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        Index("ix_documents_space_archived", "space_id", "archived_at"),
        Index("ix_documents_space_active_page", "space_id", "archived_at", "created_at", "id"),
    )


class DocumentRevisionModel(Base):
    __tablename__ = "document_revisions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    space_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    staging_storage_key: Mapped[str | None] = mapped_column(Text)
    storage_key: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default=text("'pending'"), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_by_member_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("version > 0", name="ck_document_revisions_version"),
        CheckConstraint("size_bytes >= 0", name="ck_document_revisions_size"),
        CheckConstraint(
            "status IN ('pending','processing','ready','active','failed','quarantined','cancelled')",
            name="ck_document_revisions_status",
        ),
        CheckConstraint(
            "status <> 'active' OR (storage_key IS NOT NULL AND ready_at IS NOT NULL AND activated_at IS NOT NULL)",
            name="ck_document_revisions_active_artifacts",
        ),
        ForeignKeyConstraint(
            ["document_id", "space_id"],
            ["documents.id", "documents.space_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("document_id", "version", name="uq_document_revisions_document_version"),
        UniqueConstraint("id", "document_id", "space_id", name="uq_document_revisions_identity_parent_space"),
        UniqueConstraint("id", "space_id", name="uq_document_revisions_id_space"),
        Index("ix_document_revisions_space_status", "space_id", "status"),
        Index("ix_document_revisions_space_checksum", "space_id", "checksum_sha256"),
    )


class DocumentRevisionChunkModel(Base):
    __tablename__ = "document_revision_chunks"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_revision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    space_id: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str | None] = mapped_column(String(24))
    parser_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="ck_document_revision_chunks_index"),
        ForeignKeyConstraint(
            ["document_revision_id", "document_id", "space_id"],
            ["document_revisions.id", "document_revisions.document_id", "document_revisions.space_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("document_revision_id", "chunk_index", name="uq_document_revision_chunks_revision_index"),
        UniqueConstraint("id", "space_id", name="uq_document_revision_chunks_id_space"),
        Index("ix_document_revision_chunks_space_document", "space_id", "document_id"),
    )


class EmbeddingGenerationModel(Base):
    __tablename__ = "embedding_generations"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="building", server_default=text("'building'"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("dimensions > 0", name="ck_embedding_generations_dimensions"),
        CheckConstraint("status IN ('building','active','retired','failed')", name="ck_embedding_generations_status"),
        Index("uq_embedding_generations_active_purpose", "purpose", unique=True, postgresql_where=text("status = 'active'")),
    )


class RetrievalUnitModel(Base):
    __tablename__ = "retrieval_units"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    knowledge_revision_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    document_revision_chunk_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    embedding_generation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("embedding_generations.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(24))
    source_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    tsv: Mapped[str | None] = mapped_column(TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True))
    active: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "(source_type = 'knowledge_revision' AND knowledge_revision_id IS NOT NULL AND document_revision_chunk_id IS NULL) OR "
            "(source_type = 'document_chunk' AND knowledge_revision_id IS NULL AND document_revision_chunk_id IS NOT NULL)",
            name="ck_retrieval_units_source",
        ),
        ForeignKeyConstraint(
            ["knowledge_revision_id", "space_id"],
            ["knowledge_revisions.id", "knowledge_revisions.space_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["document_revision_chunk_id", "space_id"],
            ["document_revision_chunks.id", "document_revision_chunks.space_id"],
            ondelete="CASCADE",
        ),
        Index("ix_retrieval_units_space_active", "space_id", "active"),
        Index(
            "ix_retrieval_units_active_generation_space",
            "embedding_generation_id",
            "space_id",
            postgresql_where=text("active"),
        ),
        Index("ix_retrieval_units_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_retrieval_units_content_trgm",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        ),
        Index(
            "ix_retrieval_units_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_retrieval_units_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index(
            "uq_retrieval_units_active_knowledge_generation",
            "knowledge_revision_id",
            "embedding_generation_id",
            unique=True,
            postgresql_where=text("active AND source_type = 'knowledge_revision'"),
        ),
        Index(
            "uq_retrieval_units_active_chunk_generation",
            "document_revision_chunk_id",
            "embedding_generation_id",
            unique=True,
            postgresql_where=text("active AND source_type = 'document_chunk'"),
        ),
    )


class ProvenanceLinkModel(Base):
    __tablename__ = "provenance_links"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    knowledge_revision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    document_revision_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    document_revision_chunk_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    actor_member_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="SET NULL"))
    source_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "(source_type IN ('manual','ai_action') AND document_revision_id IS NULL AND document_revision_chunk_id IS NULL) OR "
            "(source_type = 'document_revision' AND document_revision_id IS NOT NULL AND document_revision_chunk_id IS NULL) OR "
            "(source_type = 'document_chunk' AND document_revision_id IS NULL AND document_revision_chunk_id IS NOT NULL)",
            name="ck_provenance_links_source",
        ),
        ForeignKeyConstraint(
            ["knowledge_revision_id", "space_id"],
            ["knowledge_revisions.id", "knowledge_revisions.space_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["document_revision_id", "space_id"],
            ["document_revisions.id", "document_revisions.space_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["document_revision_chunk_id", "space_id"],
            ["document_revision_chunks.id", "document_revision_chunks.space_id"],
            ondelete="RESTRICT",
        ),
        Index("ix_provenance_links_knowledge", "knowledge_revision_id"),
    )


class IngestionJobModel(Base):
    __tablename__ = "ingestion_jobs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_revision_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    initiated_by_member_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="RESTRICT"), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), default="document_ingestion", server_default=text("'document_ingestion'"), nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="queued", server_default=text("'queued'"), nullable=False)
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"), nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    retry_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("state IN ('preparing','queued','running','retry_wait','succeeded','failed','cancelled')", name="ck_ingestion_jobs_state"),
        CheckConstraint("progress BETWEEN 0 AND 100", name="ck_ingestion_jobs_progress"),
        CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_ingestion_jobs_attempts"),
        CheckConstraint(
            "request_fingerprint IS NULL OR length(request_fingerprint) = 64",
            name="ck_ingestion_jobs_request_fingerprint",
        ),
        CheckConstraint(
            "(state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND claim_token IS NOT NULL) OR "
            "(state <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL AND claim_token IS NULL)",
            name="ck_ingestion_jobs_claim",
        ),
        CheckConstraint(
            "(state IN ('succeeded','failed','cancelled') AND finished_at IS NOT NULL) OR "
            "(state NOT IN ('succeeded','failed','cancelled') AND finished_at IS NULL)",
            name="ck_ingestion_jobs_terminal",
        ),
        ForeignKeyConstraint(
            ["document_revision_id", "document_id", "space_id"],
            ["document_revisions.id", "document_revisions.document_id", "document_revisions.space_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("initiated_by_member_id", "idempotency_key", name="uq_ingestion_jobs_actor_idempotency"),
        Index("ix_ingestion_jobs_claimable", "state", "next_attempt_at", "created_at"),
        Index("ix_ingestion_jobs_space_created", "space_id", "created_at"),
        Index("ix_ingestion_jobs_space_state_page", "space_id", "state", "created_at", "id"),
    )


class JobOutboxModel(Base):
    __tablename__ = "job_outbox"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("ingestion_jobs.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="pending", server_default=text("'pending'"), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8, server_default=text("8"), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("state IN ('pending','publishing','published','failed')", name="ck_job_outbox_state"),
        CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_job_outbox_attempts"),
        CheckConstraint(
            "(state = 'publishing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND claim_token IS NOT NULL) OR "
            "(state <> 'publishing' AND lease_owner IS NULL AND lease_expires_at IS NULL AND claim_token IS NULL)",
            name="ck_job_outbox_claim",
        ),
        UniqueConstraint("deduplication_key", name="uq_job_outbox_deduplication_key"),
        Index("ix_job_outbox_pending", "state", "available_at"),
    )


class MigrationBackfillRunModel(Base):
    __tablename__ = "migration_backfill_runs"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    phase: Mapped[str] = mapped_column(String(16), default="snapshot", server_default=text("'snapshot'"), nullable=False)
    high_water_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    high_water_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    delta_high_water_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delta_high_water_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    cursor_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    rows_migrated: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("phase IN ('snapshot','delta','complete','failed')", name="ck_migration_backfill_runs_phase"),
        CheckConstraint("rows_migrated >= 0", name="ck_migration_backfill_runs_rows"),
    )


class OperationalAlertModel(Base):
    __tablename__ = "operational_alerts"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_member_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="SET NULL"))

    __table_args__ = (
        CheckConstraint("severity IN ('warning','error','critical')", name="ck_operational_alerts_severity"),
        Index(
            "uq_operational_alerts_open_resource",
            "code",
            "resource_type",
            "resource_id",
            unique=True,
            postgresql_where=text("acknowledged_at IS NULL"),
        ),
        Index("ix_operational_alerts_unacknowledged", "created_at", postgresql_where=text("acknowledged_at IS NULL")),
    )
