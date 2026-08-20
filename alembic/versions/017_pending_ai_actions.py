from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_ai_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("proposed_by_kind", sa.String(length=24), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("normalized_command", postgresql.JSONB(), nullable=False),
        sa.Column("command_hash", sa.String(length=64), nullable=False),
        sa.Column("target_ids", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("expected_revision", sa.BigInteger()),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("confirmed_by_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="SET NULL")),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("length(command_hash) = 64", name="ck_pending_ai_actions_command_hash"),
        sa.CheckConstraint("state IN ('pending','executed','expired','cancelled')", name="ck_pending_ai_actions_state"),
    )
    op.create_index("ix_pending_ai_actions_actor_member_id", "pending_ai_actions", ["actor_member_id"])
    op.create_index("ix_pending_ai_actions_actor_state_expiry", "pending_ai_actions", ["actor_member_id", "state", "expires_at"])
    op.execute("ALTER TABLE pending_ai_actions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE pending_ai_actions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY pending_ai_actions_actor_select ON pending_ai_actions FOR SELECT USING ("
        "actor_member_id::text = current_setting('app.principal_id', true) AND gateway_actor_active())"
    )
    op.execute(
        "CREATE POLICY pending_ai_actions_actor_insert ON pending_ai_actions FOR INSERT WITH CHECK ("
        "actor_member_id::text = current_setting('app.principal_id', true) AND gateway_actor_active())"
    )
    op.execute(
        "CREATE POLICY pending_ai_actions_actor_update ON pending_ai_actions FOR UPDATE USING ("
        "actor_member_id::text = current_setting('app.principal_id', true) AND gateway_actor_active()) WITH CHECK ("
        "actor_member_id::text = current_setting('app.principal_id', true) AND gateway_actor_active())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS pending_ai_actions_actor_update ON pending_ai_actions")
    op.execute("DROP POLICY IF EXISTS pending_ai_actions_actor_insert ON pending_ai_actions")
    op.execute("DROP POLICY IF EXISTS pending_ai_actions_actor_select ON pending_ai_actions")
    op.execute("ALTER TABLE pending_ai_actions NO FORCE ROW LEVEL SECURITY")
    op.drop_index("ix_pending_ai_actions_actor_state_expiry", table_name="pending_ai_actions")
    op.drop_index("ix_pending_ai_actions_actor_member_id", table_name="pending_ai_actions")
    op.drop_table("pending_ai_actions")
