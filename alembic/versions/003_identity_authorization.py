from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "members",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("username_normalized", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("system_role", sa.String(24), nullable=False),
        sa.Column("force_password_change", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username_normalized", name="uq_members_username_normalized"),
        sa.CheckConstraint("status IN ('invited','active','disabled')", name="ck_members_status"),
        sa.CheckConstraint("system_role IN ('super_admin','member')", name="ck_members_system_role"),
    )
    op.create_table(
        "password_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("temporary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_password_credentials_current_member",
        "password_credentials",
        ["member_id"],
        unique=True,
        postgresql_where=sa.text("retired_at IS NULL"),
    )
    op.create_table(
        "mfa_factors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("factor_type", sa.String(16), nullable=False),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("factor_type IN ('totp')", name="ck_mfa_factors_type"),
    )
    op.create_table(
        "mfa_recovery_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("factor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["factor_id"], ["mfa_factors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("factor_id", "code_digest", name="uq_mfa_recovery_factor_digest"),
    )
    op.create_table(
        "session_families",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("idle_expires_at <= absolute_expires_at", name="ck_session_families_expiry_order"),
    )
    op.create_index("ix_session_families_member", "session_families", ["member_id"])
    op.create_table(
        "session_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("credential_type", sa.String(16), nullable=False),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["family_id"], ["session_families.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["replaced_by_id"], ["session_credentials.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_digest", name="uq_session_credentials_digest"),
        sa.CheckConstraint("credential_type IN ('access','refresh')", name="ck_session_credentials_type"),
    )
    op.create_table(
        "personal_api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("public_id", sa.String(32), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_personal_api_keys_public_id"),
        sa.CheckConstraint("status IN ('active','revoked','expired')", name="ck_personal_api_keys_status"),
    )
    op.create_index("ix_personal_api_keys_member", "personal_api_keys", ["member_id"])
    op.create_table(
        "api_key_scopes",
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["api_key_id"], ["personal_api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("api_key_id", "scope"),
    )
    op.add_column("workspaces", sa.Column("created_by_member_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("workspaces", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workspaces", sa.Column("revision", sa.BigInteger(), server_default=sa.text("1"), nullable=False))
    op.create_foreign_key(
        "fk_workspaces_created_by_member_id",
        "workspaces",
        "members",
        ["created_by_member_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute("UPDATE workspaces SET name = 'Family Shared' WHERE id = 'global'")
    op.create_table(
        "space_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_id", sa.String(64), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["space_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["member_id"], ["members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("space_id", "member_id", name="uq_space_memberships_space_member"),
        sa.CheckConstraint("role IN ('owner','editor','reader')", name="ck_space_memberships_role"),
    )
    op.create_index("ix_space_memberships_member", "space_memberships", ["member_id"])
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_kind", sa.String(24), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=True),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(["actor_member_id"], ["members.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("outcome IN ('success','denied','failed')", name="ck_audit_events_outcome"),
    )
    op.create_index("ix_audit_events_actor_time", "audit_events", ["actor_member_id", "occurred_at"])
    op.create_table(
        "idempotency_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("resource_ids", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("actor_id", "operation", "idempotency_key", name="uq_idempotency_actor_operation_key"),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="ck_idempotency_request_hash"),
        sa.CheckConstraint("response_status IS NULL OR response_status BETWEEN 100 AND 599", name="ck_idempotency_response_status"),
    )
    op.create_table(
        "compatibility_principals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_compatibility_principals_name"),
        sa.UniqueConstraint("key_digest", name="uq_compatibility_principals_digest"),
    )


def downgrade() -> None:
    op.drop_table("compatibility_principals")
    op.drop_table("idempotency_records")
    op.drop_index("ix_audit_events_actor_time", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_space_memberships_member", table_name="space_memberships")
    op.drop_table("space_memberships")
    op.drop_constraint("fk_workspaces_created_by_member_id", "workspaces", type_="foreignkey")
    op.drop_column("workspaces", "revision")
    op.drop_column("workspaces", "archived_at")
    op.drop_column("workspaces", "created_by_member_id")
    op.drop_table("api_key_scopes")
    op.drop_index("ix_personal_api_keys_member", table_name="personal_api_keys")
    op.drop_table("personal_api_keys")
    op.drop_table("session_credentials")
    op.drop_index("ix_session_families_member", table_name="session_families")
    op.drop_table("session_families")
    op.drop_table("mfa_recovery_codes")
    op.drop_table("mfa_factors")
    op.drop_index("uq_password_credentials_current_member", table_name="password_credentials")
    op.drop_table("password_credentials")
    op.drop_table("members")
