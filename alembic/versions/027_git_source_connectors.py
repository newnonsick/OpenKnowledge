from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "027"
down_revision: Union[str, None] = "026"
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
    op.create_table(
        "source_connectors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), server_default=sa.text("'git'"), nullable=False),
        sa.Column("repo_root", sa.Text(), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("last_synced_commit", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_by_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind = 'git'", name="ck_source_connectors_kind"),
        sa.CheckConstraint(
            "status IN ('active','deleted')",
            name="ck_source_connectors_status",
        ),
        sa.ForeignKeyConstraint(["space_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_member_id"], ["members.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "space_id", name="uq_source_connectors_id_space"),
        sa.UniqueConstraint("space_id", "repo_root", "branch", name="uq_source_connectors_space_repo_branch"),
    )
    op.create_index("ix_source_connectors_space", "source_connectors", ["space_id"])
    op.create_table(
        "source_connector_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connector_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(length=64), nullable=False),
        sa.Column("repo_path", sa.Text(), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("last_synced_checksum", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["connector_id", "space_id"],
            ["source_connectors.id", "source_connectors.space_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "space_id"],
            ["documents.id", "documents.space_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connector_id", "repo_path", name="uq_source_connector_files_connector_path"),
        sa.UniqueConstraint("connector_id", "document_id", name="uq_source_connector_files_connector_document"),
    )
    op.create_index("ix_source_connector_files_connector", "source_connector_files", ["connector_id"])
    _content_policies("source_connectors")
    _content_policies("source_connector_files")


def downgrade() -> None:
    for table in ("source_connector_files", "source_connectors"):
        op.execute(f"DROP POLICY IF EXISTS {table}_member_delete ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_member_update ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_member_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_member_select ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_source_connector_files_connector", table_name="source_connector_files")
    op.drop_table("source_connector_files")
    op.drop_index("ix_source_connectors_space", table_name="source_connectors")
    op.drop_table("source_connectors")
