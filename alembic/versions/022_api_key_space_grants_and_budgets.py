from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "022"
down_revision: Union[str, None] = "021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_key_space_grants",
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["personal_api_keys.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["space_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("api_key_id", "space_id"),
    )
    op.create_index(
        "ix_api_key_space_grants_space",
        "api_key_space_grants",
        ["space_id"],
    )
    op.create_table(
        "api_key_budget_usage",
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("storage_bytes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["personal_api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("api_key_id", "space_id", "window_started_at"),
        sa.CheckConstraint("request_count >= 0", name="ck_api_key_budget_usage_requests"),
        sa.CheckConstraint("token_count >= 0", name="ck_api_key_budget_usage_tokens"),
        sa.CheckConstraint("storage_bytes >= 0", name="ck_api_key_budget_usage_storage"),
    )
    op.create_index(
        "ix_api_key_budget_usage_key_window",
        "api_key_budget_usage",
        ["api_key_id", "window_started_at"],
    )
    op.execute("REVOKE ALL ON api_key_space_grants FROM PUBLIC")
    op.execute("REVOKE ALL ON api_key_budget_usage FROM PUBLIC")
    op.execute(
        "CREATE FUNCTION gateway_key_space_grants(target_key_id uuid) "
        "RETURNS text[] LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        "AS $$ "
        "SELECT COALESCE(array_agg(space_id ORDER BY space_id), '{}'::text[]) "
        "FROM public.api_key_space_grants WHERE api_key_id = target_key_id "
        "$$"
    )
    op.execute("REVOKE ALL ON FUNCTION gateway_key_space_grants(uuid) FROM PUBLIC")
    op.execute(
        "CREATE FUNCTION gateway_key_may_use_space(target_key_id uuid, target_space_id text) "
        "RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        "AS $$ "
        "SELECT NOT EXISTS (SELECT 1 FROM public.api_key_space_grants WHERE api_key_id = target_key_id) "
        "OR EXISTS (SELECT 1 FROM public.api_key_space_grants "
        "WHERE api_key_id = target_key_id AND space_id = target_space_id) "
        "$$"
    )
    op.execute("REVOKE ALL ON FUNCTION gateway_key_may_use_space(uuid, text) FROM PUBLIC")
    op.execute(
        "CREATE OR REPLACE FUNCTION gateway_has_space_role(target_space_id text, accepted_roles text[]) "
        "RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        "AS $$ "
        "SELECT EXISTS ("
        "SELECT 1 FROM public.space_memberships sm "
        "JOIN public.members m ON m.id = sm.member_id "
        "JOIN public.workspaces w ON w.id = sm.space_id "
        "WHERE sm.space_id = target_space_id "
        "AND sm.member_id::text = current_setting('app.principal_id', true) "
        "AND sm.role = ANY(accepted_roles) "
        "AND m.status = 'active' "
        "AND public.gateway_actor_active() "
        "AND w.archived_at IS NULL "
        "AND ("
        "NULLIF(current_setting('app.credential_id', true), '') IS NULL "
        "OR public.gateway_key_may_use_space("
        "NULLIF(current_setting('app.credential_id', true), '')::uuid, target_space_id"
        ")"
        ")"
        ") $$"
    )
    op.execute("REVOKE ALL ON FUNCTION gateway_has_space_role(text, text[]) FROM PUBLIC")


def downgrade() -> None:
    op.execute(
        "CREATE OR REPLACE FUNCTION gateway_has_space_role(target_space_id text, accepted_roles text[]) "
        "RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        "AS $$ "
        "SELECT EXISTS ("
        "SELECT 1 FROM public.space_memberships sm "
        "JOIN public.members m ON m.id = sm.member_id "
        "JOIN public.workspaces w ON w.id = sm.space_id "
        "WHERE sm.space_id = target_space_id "
        "AND sm.member_id::text = current_setting('app.principal_id', true) "
        "AND sm.role = ANY(accepted_roles) "
        "AND m.status = 'active' "
        "AND public.gateway_actor_active() "
        "AND w.archived_at IS NULL"
        ") $$"
    )
    op.execute("REVOKE ALL ON FUNCTION gateway_has_space_role(text, text[]) FROM PUBLIC")
    op.execute("DROP FUNCTION IF EXISTS gateway_key_may_use_space(uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS gateway_key_space_grants(uuid)")
    op.drop_index("ix_api_key_budget_usage_key_window", table_name="api_key_budget_usage")
    op.drop_table("api_key_budget_usage")
    op.drop_index("ix_api_key_space_grants_space", table_name="api_key_space_grants")
    op.drop_table("api_key_space_grants")
