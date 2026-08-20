from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.gateway.infrastructure.persistence.models import Base


class RuntimeSettingRevisionModel(Base):
    __tablename__ = "runtime_setting_revisions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    base_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    values: Mapped[dict] = mapped_column(JSONB, nullable=False)
    draft_reason: Mapped[str] = mapped_column(Text, nullable=False)
    activation_reason: Mapped[str | None] = mapped_column(Text)
    created_by_member_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="RESTRICT"), nullable=False)
    activated_by_member_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("members.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_runtime_setting_revisions_revision"),
        CheckConstraint("base_revision >= 0", name="ck_runtime_setting_revisions_base_revision"),
        CheckConstraint("state IN ('draft','active','superseded')", name="ck_runtime_setting_revisions_state"),
        Index("uq_runtime_setting_revisions_active", "state", unique=True, postgresql_where=text("state = 'active'")),
    )
