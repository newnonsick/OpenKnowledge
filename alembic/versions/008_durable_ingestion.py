from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _content_policies(table: str) -> None:
    read = "gateway_has_space_role(space_id, ARRAY['owner','editor','reader'])"
    write = "gateway_has_space_role(space_id, ARRAY['owner','editor'])"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_member_select ON {table} FOR SELECT USING ({read})")
    op.execute(f"CREATE POLICY {table}_member_insert ON {table} FOR INSERT WITH CHECK ({write})")
    op.execute(
        f"CREATE POLICY {table}_member_update ON {table} "
        f"FOR UPDATE USING ({write}) WITH CHECK ({write})"
    )
    op.execute(f"CREATE POLICY {table}_member_delete ON {table} FOR DELETE USING ({write})")


def upgrade() -> None:
    op.add_column(
        "knowledge_items",
        sa.Column("tags", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column("knowledge_items", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "knowledge_items",
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint("ck_knowledge_items_revision", "knowledge_items", "revision > 0")
    op.create_unique_constraint(
        "uq_knowledge_items_id_space",
        "knowledge_items",
        ["id", "workspace_id"],
    )
    op.add_column("knowledge_revisions", sa.Column("space_id", sa.String(length=64), nullable=True))
    op.add_column("knowledge_revisions", sa.Column("title", sa.String(length=500), nullable=True))
    op.add_column(
        "knowledge_revisions",
        sa.Column("tags", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )
    op.add_column("knowledge_revisions", sa.Column("change_summary", sa.Text(), nullable=True))
    op.add_column(
        "knowledge_revisions",
        sa.Column("author_member_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        "UPDATE knowledge_revisions revision "
        "SET space_id = item.workspace_id, title = item.title "
        "FROM knowledge_items item WHERE item.id = revision.item_id"
    )
    op.execute(
        "UPDATE knowledge_items item SET "
        "revision = COALESCE(current_revision.version, 1), "
        "archived_at = CASE WHEN item.is_deleted THEN item.updated_at ELSE NULL END "
        "FROM knowledge_revisions current_revision "
        "WHERE current_revision.id = item.current_revision_id"
    )
    op.alter_column("knowledge_revisions", "space_id", nullable=False)
    op.create_foreign_key(
        "fk_knowledge_revisions_item_space",
        "knowledge_revisions",
        "knowledge_items",
        ["item_id", "space_id"],
        ["id", "workspace_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_knowledge_revisions_author_member",
        "knowledge_revisions",
        "members",
        ["author_member_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_knowledge_revisions_identity_parent_space",
        "knowledge_revisions",
        ["id", "item_id", "space_id"],
    )
    op.create_unique_constraint(
        "uq_knowledge_revisions_id_space",
        "knowledge_revisions",
        ["id", "space_id"],
    )
    op.drop_constraint(
        "fk_knowledge_items_current_revision_id",
        "knowledge_items",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_knowledge_items_current_revision_parent_space",
        "knowledge_items",
        "knowledge_revisions",
        ["current_revision_id", "id", "workspace_id"],
        ["id", "item_id", "space_id"],
        ondelete="RESTRICT",
        use_alter=True,
    )

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=500), nullable=False),
        sa.Column("current_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_documents_revision"),
        sa.ForeignKeyConstraint(["space_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_member_id"], ["members.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "space_id", name="uq_documents_id_space"),
    )
    op.create_index("ix_documents_space_archived", "documents", ["space_id", "archived_at"])

    op.create_table(
        "document_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(length=500), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("staging_storage_key", sa.Text(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("parser_version", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_by_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version > 0", name="ck_document_revisions_version"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_document_revisions_size"),
        sa.CheckConstraint(
            "status IN ('pending','processing','ready','active','failed','quarantined','cancelled')",
            name="ck_document_revisions_status",
        ),
        sa.CheckConstraint(
            "status <> 'active' OR (storage_key IS NOT NULL AND ready_at IS NOT NULL AND activated_at IS NOT NULL)",
            name="ck_document_revisions_active_artifacts",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "space_id"],
            ["documents.id", "documents.space_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by_member_id"], ["members.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "version", name="uq_document_revisions_document_version"),
        sa.UniqueConstraint(
            "id",
            "document_id",
            "space_id",
            name="uq_document_revisions_identity_parent_space",
        ),
        sa.UniqueConstraint("id", "space_id", name="uq_document_revisions_id_space"),
    )
    op.create_index("ix_document_revisions_space_status", "document_revisions", ["space_id", "status"])
    op.create_index("ix_document_revisions_space_checksum", "document_revisions", ["space_id", "checksum_sha256"])
    op.create_foreign_key(
        "fk_documents_current_revision_parent_space",
        "documents",
        "document_revisions",
        ["current_revision_id", "id", "space_id"],
        ["id", "document_id", "space_id"],
        ondelete="RESTRICT",
        use_alter=True,
    )

    op.create_table(
        "document_revision_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=24), nullable=True),
        sa.Column("parser_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("chunk_index >= 0", name="ck_document_revision_chunks_index"),
        sa.ForeignKeyConstraint(
            ["document_revision_id", "document_id", "space_id"],
            ["document_revisions.id", "document_revisions.document_id", "document_revisions.space_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_revision_id",
            "chunk_index",
            name="uq_document_revision_chunks_revision_index",
        ),
        sa.UniqueConstraint("id", "space_id", name="uq_document_revision_chunks_id_space"),
    )
    op.create_index("ix_document_revision_chunks_space_document", "document_revision_chunks", ["space_id", "document_id"])

    op.create_table(
        "embedding_generations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=255), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'building'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("dimensions > 0", name="ck_embedding_generations_dimensions"),
        sa.CheckConstraint(
            "status IN ('building','active','retired','failed')",
            name="ck_embedding_generations_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_embedding_generations_active_purpose",
        "embedding_generations",
        ["purpose"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "retrieval_units",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("knowledge_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_revision_chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("embedding_generation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=24), nullable=True),
        sa.Column("source_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(1024), nullable=True),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(source_type = 'knowledge_revision' AND knowledge_revision_id IS NOT NULL AND document_revision_chunk_id IS NULL) OR "
            "(source_type = 'document_chunk' AND knowledge_revision_id IS NULL AND document_revision_chunk_id IS NOT NULL)",
            name="ck_retrieval_units_source",
        ),
        sa.ForeignKeyConstraint(["space_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["knowledge_revision_id", "space_id"],
            ["knowledge_revisions.id", "knowledge_revisions.space_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_chunk_id", "space_id"],
            ["document_revision_chunks.id", "document_revision_chunks.space_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["embedding_generation_id"], ["embedding_generations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_retrieval_units_space_active", "retrieval_units", ["space_id", "active"])
    op.create_index("ix_retrieval_units_tsv", "retrieval_units", ["tsv"], postgresql_using="gin")
    op.create_index(
        "ix_retrieval_units_embedding",
        "retrieval_units",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "uq_retrieval_units_active_knowledge_generation",
        "retrieval_units",
        ["knowledge_revision_id", "embedding_generation_id"],
        unique=True,
        postgresql_where=sa.text("active AND source_type = 'knowledge_revision'"),
    )
    op.create_index(
        "uq_retrieval_units_active_chunk_generation",
        "retrieval_units",
        ["document_revision_chunk_id", "embedding_generation_id"],
        unique=True,
        postgresql_where=sa.text("active AND source_type = 'document_chunk'"),
    )

    op.create_table(
        "provenance_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(length=64), nullable=False),
        sa.Column("knowledge_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_revision_chunk_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(source_type IN ('manual','ai_action') AND document_revision_id IS NULL AND document_revision_chunk_id IS NULL) OR "
            "(source_type = 'document_revision' AND document_revision_id IS NOT NULL AND document_revision_chunk_id IS NULL) OR "
            "(source_type = 'document_chunk' AND document_revision_id IS NULL AND document_revision_chunk_id IS NOT NULL)",
            name="ck_provenance_links_source",
        ),
        sa.ForeignKeyConstraint(["space_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["knowledge_revision_id", "space_id"],
            ["knowledge_revisions.id", "knowledge_revisions.space_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id", "space_id"],
            ["document_revisions.id", "document_revisions.space_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_chunk_id", "space_id"],
            ["document_revision_chunks.id", "document_revision_chunks.space_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["actor_member_id"], ["members.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provenance_links_knowledge", "provenance_links", ["knowledge_revision_id"])

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("initiated_by_member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_type", sa.String(length=32), server_default=sa.text("'document_ingestion'"), nullable=False),
        sa.Column("state", sa.String(length=20), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("progress", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("cancellation_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('preparing','queued','running','retry_wait','succeeded','failed','cancelled')",
            name="ck_ingestion_jobs_state",
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="ck_ingestion_jobs_progress"),
        sa.CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_ingestion_jobs_attempts"),
        sa.CheckConstraint(
            "(state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND claim_token IS NOT NULL) OR "
            "(state <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL AND claim_token IS NULL)",
            name="ck_ingestion_jobs_claim",
        ),
        sa.CheckConstraint(
            "(state IN ('succeeded','failed','cancelled') AND finished_at IS NOT NULL) OR "
            "(state NOT IN ('succeeded','failed','cancelled') AND finished_at IS NULL)",
            name="ck_ingestion_jobs_terminal",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id", "document_id", "space_id"],
            ["document_revisions.id", "document_revisions.document_id", "document_revisions.space_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["initiated_by_member_id"], ["members.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "initiated_by_member_id",
            "idempotency_key",
            name="uq_ingestion_jobs_actor_idempotency",
        ),
    )
    op.create_index("ix_ingestion_jobs_claimable", "ingestion_jobs", ["state", "next_attempt_at", "created_at"])
    op.create_index("ix_ingestion_jobs_space_created", "ingestion_jobs", ["space_id", "created_at"])

    op.create_table(
        "job_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("deduplication_key", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("state IN ('pending','published','failed')", name="ck_job_outbox_state"),
        sa.ForeignKeyConstraint(["job_id"], ["ingestion_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deduplication_key", name="uq_job_outbox_deduplication_key"),
    )
    op.create_index("ix_job_outbox_pending", "job_outbox", ["state", "available_at"])

    for table in (
        "documents",
        "document_revisions",
        "document_revision_chunks",
        "retrieval_units",
        "provenance_links",
        "ingestion_jobs",
    ):
        _content_policies(table)


def downgrade() -> None:
    for table in (
        "ingestion_jobs",
        "provenance_links",
        "retrieval_units",
        "document_revision_chunks",
        "document_revisions",
        "documents",
    ):
        for operation in ("delete", "update", "insert", "select"):
            op.execute(f"DROP POLICY IF EXISTS {table}_member_{operation} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_table("job_outbox")
    op.drop_table("ingestion_jobs")
    op.drop_table("provenance_links")
    op.drop_table("retrieval_units")
    op.drop_table("embedding_generations")
    op.drop_table("document_revision_chunks")
    op.drop_constraint("fk_documents_current_revision_parent_space", "documents", type_="foreignkey")
    op.drop_table("document_revisions")
    op.drop_table("documents")
    op.drop_constraint(
        "fk_knowledge_items_current_revision_parent_space",
        "knowledge_items",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_knowledge_items_current_revision_id",
        "knowledge_items",
        "knowledge_revisions",
        ["current_revision_id"],
        ["id"],
        ondelete="SET NULL",
        use_alter=True,
    )
    op.drop_constraint(
        "uq_knowledge_revisions_id_space",
        "knowledge_revisions",
        type_="unique",
    )
    op.drop_constraint(
        "uq_knowledge_revisions_identity_parent_space",
        "knowledge_revisions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_knowledge_revisions_author_member",
        "knowledge_revisions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_knowledge_revisions_item_space",
        "knowledge_revisions",
        type_="foreignkey",
    )
    op.drop_column("knowledge_revisions", "author_member_id")
    op.drop_column("knowledge_revisions", "change_summary")
    op.drop_column("knowledge_revisions", "tags")
    op.drop_column("knowledge_revisions", "title")
    op.drop_column("knowledge_revisions", "space_id")
    op.drop_constraint("uq_knowledge_items_id_space", "knowledge_items", type_="unique")
    op.drop_constraint("ck_knowledge_items_revision", "knowledge_items", type_="check")
    op.drop_column("knowledge_items", "revision")
    op.drop_column("knowledge_items", "archived_at")
    op.drop_column("knowledge_items", "tags")
