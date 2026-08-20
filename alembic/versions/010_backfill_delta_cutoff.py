from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "migration_backfill_runs",
        sa.Column("delta_high_water_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "migration_backfill_runs",
        sa.Column("delta_high_water_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("migration_backfill_runs", "delta_high_water_id")
    op.drop_column("migration_backfill_runs", "delta_high_water_created_at")
