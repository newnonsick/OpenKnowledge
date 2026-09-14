from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "030"
down_revision: Union[str, None] = "029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _subscription_policies() -> None:
    read = "gateway_has_space_role(space_id, ARRAY['owner','editor','reader'])"
    write = "gateway_has_space_role(space_id, ARRAY['owner','editor'])"
    op.execute("ALTER TABLE webhook_subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE webhook_subscriptions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY webhook_subscriptions_member_select ON webhook_subscriptions "
        f"FOR SELECT USING ({read})"
    )
    op.execute(
        "CREATE POLICY webhook_subscriptions_member_insert ON webhook_subscriptions "
        f"FOR INSERT WITH CHECK ({write})"
    )
    op.execute(
        "CREATE POLICY webhook_subscriptions_member_update ON webhook_subscriptions "
        f"FOR UPDATE USING ({read}) WITH CHECK ({write})"
    )
    op.execute(
        "CREATE POLICY webhook_subscriptions_member_delete ON webhook_subscriptions "
        f"FOR DELETE USING ({write})"
    )


def _delivery_policies() -> None:
    read = (
        "EXISTS (SELECT 1 FROM webhook_subscriptions s WHERE s.id = subscription_id "
        "AND gateway_has_space_role(s.space_id, ARRAY['owner','editor','reader']))"
    )
    write = (
        "EXISTS (SELECT 1 FROM webhook_subscriptions s WHERE s.id = subscription_id "
        "AND gateway_has_space_role(s.space_id, ARRAY['owner','editor']))"
    )
    op.execute("ALTER TABLE webhook_deliveries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE webhook_deliveries FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY webhook_deliveries_member_select ON webhook_deliveries "
        f"FOR SELECT USING ({read})"
    )
    op.execute(
        "CREATE POLICY webhook_deliveries_member_insert ON webhook_deliveries "
        f"FOR INSERT WITH CHECK ({write})"
    )
    op.execute(
        "CREATE POLICY webhook_deliveries_member_update ON webhook_deliveries "
        f"FOR UPDATE USING ({read}) WITH CHECK ({write})"
    )
    op.execute(
        "CREATE POLICY webhook_deliveries_member_delete ON webhook_deliveries "
        f"FOR DELETE USING ({write})"
    )


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("event_filter", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_by_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','revoked')", name="ck_webhook_subscriptions_status"),
        sa.CheckConstraint(
            "(status = 'revoked' AND revoked_at IS NOT NULL) OR "
            "(status <> 'revoked' AND revoked_at IS NULL)",
            name="ck_webhook_subscriptions_revocation",
        ),
        sa.ForeignKeyConstraint(["space_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_member_id"], ["members.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "space_id", name="uq_webhook_subscriptions_id_space"),
    )
    op.create_index("ix_webhook_subscriptions_space", "webhook_subscriptions", ["space_id"])
    op.create_index("ix_webhook_subscriptions_status", "webhook_subscriptions", ["status"])
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("deduplication_key", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("state", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("8"), nullable=False),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending','delivering','delivered','failed')",
            name="ck_webhook_deliveries_state",
        ),
        sa.CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_webhook_deliveries_attempts"),
        sa.CheckConstraint(
            "(state = 'delivering' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND claim_token IS NOT NULL) OR "
            "(state <> 'delivering' AND lease_owner IS NULL AND lease_expires_at IS NULL AND claim_token IS NULL)",
            name="ck_webhook_deliveries_claim",
        ),
        sa.ForeignKeyConstraint(["subscription_id"], ["webhook_subscriptions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subscription_id", "deduplication_key", name="uq_webhook_deliveries_subscription_key"),
    )
    op.create_index("ix_webhook_deliveries_claimable", "webhook_deliveries", ["state", "available_at"])
    op.create_index("ix_webhook_deliveries_subscription", "webhook_deliveries", ["subscription_id"])
    _subscription_policies()
    _delivery_policies()


def downgrade() -> None:
    for table in ("webhook_deliveries", "webhook_subscriptions"):
        for operation in ("delete", "update", "insert", "select"):
            op.execute(f"DROP POLICY IF EXISTS {table}_member_{operation} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_webhook_deliveries_subscription", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_claimable", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_subscriptions_status", table_name="webhook_subscriptions")
    op.drop_index("ix_webhook_subscriptions_space", table_name="webhook_subscriptions")
    op.drop_table("webhook_subscriptions")
