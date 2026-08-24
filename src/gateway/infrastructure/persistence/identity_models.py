from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.gateway.infrastructure.persistence.models import Base


class MemberModel(Base):
    __tablename__ = "members"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    username_normalized: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    system_role: Mapped[str] = mapped_column(String(24), nullable=False)
    force_password_change: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("username_normalized", name="uq_members_username_normalized"),
        CheckConstraint("status IN ('pending','active','disabled')", name="ck_members_status"),
        CheckConstraint("system_role IN ('super_admin','member')", name="ck_members_system_role"),
    )


class PasswordCredentialModel(Base):
    __tablename__ = "password_credentials"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    member_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    temporary: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_password_credentials_current_member", "member_id", unique=True, postgresql_where=text("retired_at IS NULL")),
    )


class MFAFactorModel(Base):
    __tablename__ = "mfa_factors"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    member_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=False)
    factor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encryption_key_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("factor_type IN ('totp')", name="ck_mfa_factors_type"),)


class MFARecoveryCodeModel(Base):
    __tablename__ = "mfa_recovery_codes"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    factor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("mfa_factors.id", ondelete="CASCADE"), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("factor_id", "code_digest", name="uq_mfa_recovery_factor_digest"),)


class SessionFamilyModel(Base):
    __tablename__ = "session_families"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    member_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason: Mapped[str | None] = mapped_column(String(64))
    csrf_token_digest: Mapped[str | None] = mapped_column(String(64))
    last_step_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("idle_expires_at <= absolute_expires_at", name="ck_session_families_expiry_order"),
        CheckConstraint("csrf_token_digest IS NOT NULL OR revoked_at IS NOT NULL", name="ck_session_families_csrf_or_revoked"),
        Index("ix_session_families_member_page", "member_id", "created_at", "id"),
    )


class SessionCredentialModel(Base):
    __tablename__ = "session_credentials"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("session_families.id", ondelete="CASCADE"), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(16), nullable=False)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("session_credentials.id", ondelete="SET NULL"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("credential_type IN ('access','refresh')", name="ck_session_credentials_type"),)


class PersonalAPIKeyModel(Base):
    __tablename__ = "personal_api_keys"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    member_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    public_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    pepper_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('active','revoked','expired')", name="ck_personal_api_keys_status"),
        CheckConstraint("pepper_version > 0", name="ck_personal_api_keys_pepper_version"),
        Index("ix_personal_api_keys_member_status_page", "member_id", "status", "created_at", "id"),
    )


class APIKeyScopeModel(Base):
    __tablename__ = "api_key_scopes"

    api_key_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("personal_api_keys.id", ondelete="CASCADE"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)


class SpaceMembershipModel(Base):
    __tablename__ = "space_memberships"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    space_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    member_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("space_id", "member_id", name="uq_space_memberships_space_member"),
        CheckConstraint("role IN ('owner','editor','reader')", name="ck_space_memberships_role"),
    )


class AuditEventModel(Base):
    __tablename__ = "audit_events"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    actor_member_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="SET NULL"))
    actor_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"), nullable=False)

    __table_args__ = (
        Index("ix_audit_events_actor_time", "actor_member_id", "occurred_at"),
        Index("ix_audit_events_page", "occurred_at", "id"),
        CheckConstraint("outcome IN ('success','denied','failed')", name="ck_audit_events_outcome"),
    )


class IdempotencyRecordModel(Base):
    __tablename__ = "idempotency_records"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    resource_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("actor_id", "operation", "idempotency_key", name="uq_idempotency_actor_operation_key"),
        CheckConstraint("length(request_hash) = 64", name="ck_idempotency_request_hash"),
        CheckConstraint("response_status IS NULL OR response_status BETWEEN 100 AND 599", name="ck_idempotency_response_status"),
    )


class PendingAIActionModel(Base):
    __tablename__ = "pending_ai_actions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    actor_member_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="CASCADE"), nullable=False, index=True)
    proposed_by_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_command: Mapped[dict] = mapped_column(JSONB, nullable=False)
    command_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_ids: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False)
    expected_revision: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    confirmed_by_member_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="SET NULL"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("length(command_hash) = 64", name="ck_pending_ai_actions_command_hash"),
        CheckConstraint("state IN ('pending','executed','expired','cancelled')", name="ck_pending_ai_actions_state"),
        Index("ix_pending_ai_actions_actor_state_expiry", "actor_member_id", "state", "expires_at"),
        Index("ix_pending_ai_actions_actor_pending_page", "actor_member_id", "state", "created_at", "id"),
    )


class CompatibilityPrincipalModel(Base):
    __tablename__ = "compatibility_principals"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginThrottleBucketModel(Base):
    __tablename__ = "login_throttle_buckets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bucket_type: Mapped[str] = mapped_column(String(16), nullable=False)
    bucket_key: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("bucket_type", "bucket_key", name="uq_login_throttle_bucket"),
        CheckConstraint("bucket_type IN ('account','ip','global')", name="ck_login_throttle_bucket_type"),
        CheckConstraint("failure_count >= 0", name="ck_login_throttle_failure_count"),
    )
