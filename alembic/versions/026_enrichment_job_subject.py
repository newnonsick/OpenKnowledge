from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "026"
down_revision: Union[str, None] = "025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("knowledge_item_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "ingestion_jobs",
        sa.Column("knowledge_revision_id", sa.UUID(as_uuid=True), nullable=True),
    )
    op.alter_column("ingestion_jobs", "document_id", existing_type=sa.UUID(as_uuid=True), nullable=True)
    op.alter_column("ingestion_jobs", "document_revision_id", existing_type=sa.UUID(as_uuid=True), nullable=True)
    op.create_check_constraint(
        "ck_ingestion_jobs_subject",
        "ingestion_jobs",
        "(job_type = 'knowledge_enrichment' AND document_id IS NULL AND document_revision_id IS NULL "
        "AND knowledge_item_id IS NOT NULL AND knowledge_revision_id IS NOT NULL) OR "
        "((job_type IS NULL OR job_type <> 'knowledge_enrichment') AND document_id IS NOT NULL "
        "AND document_revision_id IS NOT NULL AND knowledge_item_id IS NULL AND knowledge_revision_id IS NULL)",
    )
    op.create_foreign_key(
        "fk_ingestion_jobs_knowledge_revision_space",
        "ingestion_jobs",
        "knowledge_revisions",
        ["knowledge_revision_id", "knowledge_item_id", "space_id"],
        ["id", "item_id", "space_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_ingestion_jobs_knowledge_revision",
        "ingestion_jobs",
        ["knowledge_revision_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_knowledge_revision", table_name="ingestion_jobs")
    op.drop_constraint("fk_ingestion_jobs_knowledge_revision_space", "ingestion_jobs", type_="foreignkey")
    op.drop_constraint("ck_ingestion_jobs_subject", "ingestion_jobs", type_="check")
    op.execute("DELETE FROM ingestion_jobs WHERE job_type = 'knowledge_enrichment'")
    op.alter_column("ingestion_jobs", "document_revision_id", existing_type=sa.UUID(as_uuid=True), nullable=False)
    op.alter_column("ingestion_jobs", "document_id", existing_type=sa.UUID(as_uuid=True), nullable=False)
    op.drop_column("ingestion_jobs", "knowledge_revision_id")
    op.drop_column("ingestion_jobs", "knowledge_item_id")
