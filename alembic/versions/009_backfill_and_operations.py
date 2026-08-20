from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "migration_backfill_runs",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("phase", sa.String(length=16), server_default=sa.text("'snapshot'"), nullable=False),
        sa.Column("high_water_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("high_water_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cursor_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rows_migrated", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "phase IN ('snapshot','delta','complete','failed')",
            name="ck_migration_backfill_runs_phase",
        ),
        sa.CheckConstraint("rows_migrated >= 0", name="ck_migration_backfill_runs_rows"),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "operational_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "acknowledged_by_member_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("members.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "severity IN ('warning','error','critical')",
            name="ck_operational_alerts_severity",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_operational_alerts_open_resource",
        "operational_alerts",
        ["code", "resource_type", "resource_id"],
        unique=True,
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )
    op.create_index(
        "ix_operational_alerts_unacknowledged",
        "operational_alerts",
        ["created_at"],
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )
    op.add_column(
        "job_outbox",
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "job_outbox",
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("8"), nullable=False),
    )
    op.add_column("job_outbox", sa.Column("last_error_code", sa.String(length=64), nullable=True))
    op.add_column(
        "job_outbox",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_check_constraint(
        "ck_job_outbox_attempts",
        "job_outbox",
        "attempt_count >= 0 AND max_attempts > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_job_outbox_attempts", "job_outbox", type_="check")
    op.drop_column("job_outbox", "updated_at")
    op.drop_column("job_outbox", "last_error_code")
    op.drop_column("job_outbox", "max_attempts")
    op.drop_column("job_outbox", "attempt_count")
    op.drop_index("ix_operational_alerts_unacknowledged", table_name="operational_alerts")
    op.drop_index("uq_operational_alerts_open_resource", table_name="operational_alerts")
    op.drop_table("operational_alerts")
    op.drop_table("migration_backfill_runs")
