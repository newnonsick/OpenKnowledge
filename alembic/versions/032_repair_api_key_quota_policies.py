from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "032"
down_revision: Union[str, None] = "031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_key_quota_policies",
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(64), nullable=False, server_default=sa.text("'*'")),
        sa.Column("requests_per_minute", sa.Integer(), nullable=True),
        sa.Column("tokens_per_minute", sa.Integer(), nullable=True),
        sa.Column("storage_bytes", sa.Integer(), nullable=True),
        sa.Column("concurrent_requests", sa.Integer(), nullable=True),
        sa.Column("burst_requests", sa.Integer(), nullable=True),
        sa.Column("window_seconds", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["personal_api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("api_key_id", "space_id"),
        sa.CheckConstraint(
            "requests_per_minute IS NULL OR requests_per_minute > 0",
            name="ck_api_key_quota_policies_requests",
        ),
        sa.CheckConstraint(
            "tokens_per_minute IS NULL OR tokens_per_minute > 0",
            name="ck_api_key_quota_policies_tokens",
        ),
        sa.CheckConstraint(
            "storage_bytes IS NULL OR storage_bytes > 0",
            name="ck_api_key_quota_policies_storage",
        ),
        sa.CheckConstraint(
            "concurrent_requests IS NULL OR concurrent_requests > 0",
            name="ck_api_key_quota_policies_concurrency",
        ),
        sa.CheckConstraint(
            "burst_requests IS NULL OR burst_requests >= 0",
            name="ck_api_key_quota_policies_burst",
        ),
        sa.CheckConstraint(
            "window_seconds IS NULL OR window_seconds > 0",
            name="ck_api_key_quota_policies_window",
        ),
        if_not_exists=True,
    )
    op.create_index(
        "ix_api_key_quota_policies_key",
        "api_key_quota_policies",
        ["api_key_id"],
        if_not_exists=True,
    )
    op.execute("REVOKE ALL ON api_key_quota_policies FROM PUBLIC")


def downgrade() -> None:
    op.drop_index("ix_api_key_quota_policies_key", table_name="api_key_quota_policies", if_exists=True)
    op.drop_table("api_key_quota_policies", if_exists=True)
