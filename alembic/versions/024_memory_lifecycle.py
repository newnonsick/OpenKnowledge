from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "024"
down_revision: Union[str, None] = "023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "knowledge_items",
        sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="accepted"),
    )
    op.add_column(
        "knowledge_items",
        sa.Column("origin", sa.String(200), nullable=True),
    )
    op.add_column(
        "knowledge_items",
        sa.Column("source_detail", sa.Text(), nullable=True),
    )
    op.add_column(
        "knowledge_items",
        sa.Column("review_note", sa.Text(), nullable=True),
    )
    op.add_column(
        "knowledge_items",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_knowledge_items_lifecycle_status",
        "knowledge_items",
        "lifecycle_status IN ('observation', 'candidate', 'accepted', 'superseded')",
    )
    op.create_index(
        "ix_knowledge_items_workspace_lifecycle",
        "knowledge_items",
        ["workspace_id", "lifecycle_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_items_workspace_lifecycle", table_name="knowledge_items")
    op.drop_constraint("ck_knowledge_items_lifecycle_status", "knowledge_items", type_="check")
    op.drop_column("knowledge_items", "expires_at")
    op.drop_column("knowledge_items", "review_note")
    op.drop_column("knowledge_items", "source_detail")
    op.drop_column("knowledge_items", "origin")
    op.drop_column("knowledge_items", "lifecycle_status")
