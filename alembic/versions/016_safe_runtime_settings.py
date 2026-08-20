from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_setting_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("base_revision", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("values", postgresql.JSONB(), nullable=False),
        sa.Column("draft_reason", sa.Text(), nullable=False),
        sa.Column("activation_reason", sa.Text()),
        sa.Column("created_by_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("activated_by_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("members.id", ondelete="RESTRICT")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("revision", name="uq_runtime_setting_revisions_revision"),
        sa.CheckConstraint("revision > 0", name="ck_runtime_setting_revisions_revision"),
        sa.CheckConstraint("base_revision >= 0", name="ck_runtime_setting_revisions_base_revision"),
        sa.CheckConstraint("state IN ('draft','active','superseded')", name="ck_runtime_setting_revisions_state"),
    )
    op.create_index(
        "uq_runtime_setting_revisions_active",
        "runtime_setting_revisions",
        ["state"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )
    op.execute(
        "CREATE FUNCTION gateway_actor_super_admin() RETURNS boolean "
        "LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        "AS $$ SELECT EXISTS ("
        "SELECT 1 FROM public.members m "
        "WHERE m.id::text = current_setting('app.principal_id', true) "
        "AND m.status = 'active' AND m.system_role = 'super_admin' "
        "AND public.gateway_actor_active()"
        ") $$"
    )
    op.execute("REVOKE ALL ON FUNCTION gateway_actor_super_admin() FROM PUBLIC")
    op.execute("ALTER TABLE runtime_setting_revisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE runtime_setting_revisions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY runtime_setting_revisions_member_select ON runtime_setting_revisions "
        "FOR SELECT USING (gateway_actor_active())"
    )
    op.execute(
        "CREATE POLICY runtime_setting_revisions_admin_insert ON runtime_setting_revisions "
        "FOR INSERT WITH CHECK (gateway_actor_super_admin() "
        "AND created_by_member_id::text = current_setting('app.principal_id', true))"
    )
    op.execute(
        "CREATE POLICY runtime_setting_revisions_admin_update ON runtime_setting_revisions "
        "FOR UPDATE USING (gateway_actor_super_admin()) "
        "WITH CHECK (gateway_actor_super_admin())"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS runtime_setting_revisions_admin_update ON runtime_setting_revisions")
    op.execute("DROP POLICY IF EXISTS runtime_setting_revisions_admin_insert ON runtime_setting_revisions")
    op.execute("DROP POLICY IF EXISTS runtime_setting_revisions_member_select ON runtime_setting_revisions")
    op.execute("ALTER TABLE runtime_setting_revisions NO FORCE ROW LEVEL SECURITY")
    op.drop_index("uq_runtime_setting_revisions_active", table_name="runtime_setting_revisions")
    op.drop_table("runtime_setting_revisions")
    op.execute("DROP FUNCTION IF EXISTS gateway_actor_super_admin()")
