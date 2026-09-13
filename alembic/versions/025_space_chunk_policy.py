from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "025"
down_revision: Union[str, None] = "024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("chunk_size", sa.Integer(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("chunk_overlap", sa.Integer(), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("chunk_strategy", sa.String(16), nullable=True),
    )
    op.create_check_constraint(
        "ck_workspaces_chunk_policy",
        "workspaces",
        "(chunk_size IS NULL AND chunk_overlap IS NULL AND chunk_strategy IS NULL) OR "
        "(chunk_size IS NOT NULL AND chunk_overlap IS NOT NULL AND chunk_strategy IS NOT NULL "
        "AND chunk_size BETWEEN 64 AND 32000 "
        "AND chunk_overlap >= 0 AND chunk_overlap < chunk_size "
        "AND chunk_strategy IN ('fixed', 'semantic'))",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_chunk_policy", "workspaces", type_="check")
    op.drop_column("workspaces", "chunk_strategy")
    op.drop_column("workspaces", "chunk_overlap")
    op.drop_column("workspaces", "chunk_size")
