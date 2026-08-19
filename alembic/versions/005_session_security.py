from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("session_families", sa.Column("csrf_token_digest", sa.String(64), nullable=True))
    op.add_column("session_families", sa.Column("last_step_up_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("session_credentials", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(
        "UPDATE session_families SET revoked_at = now(), revoke_reason = 'schema_upgrade' "
        "WHERE csrf_token_digest IS NULL AND revoked_at IS NULL"
    )
    op.create_check_constraint(
        "ck_session_families_csrf_or_revoked",
        "session_families",
        "csrf_token_digest IS NOT NULL OR revoked_at IS NOT NULL",
    )
    op.create_table(
        "login_throttle_buckets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("bucket_type", sa.String(16), nullable=False),
        sa.Column("bucket_key", sa.String(64), nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bucket_type", "bucket_key", name="uq_login_throttle_bucket"),
        sa.CheckConstraint("bucket_type IN ('account','ip','global')", name="ck_login_throttle_bucket_type"),
        sa.CheckConstraint("failure_count >= 0", name="ck_login_throttle_failure_count"),
    )


def downgrade() -> None:
    op.drop_table("login_throttle_buckets")
    op.drop_constraint("ck_session_families_csrf_or_revoked", "session_families", type_="check")
    op.drop_column("session_credentials", "revoked_at")
    op.drop_column("session_families", "last_step_up_at")
    op.drop_column("session_families", "csrf_token_digest")
