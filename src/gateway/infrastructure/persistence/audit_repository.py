from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.infrastructure.persistence.identity_models import AuditEventModel


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def append(
        self,
        *,
        actor_member_id: UUID | None,
        actor_kind: str,
        request_id: str,
        action: str,
        resource_type: str,
        resource_id: str | None,
        outcome: str,
        details: dict | None = None,
    ) -> AuditEventModel:
        event = AuditEventModel(
            actor_member_id=actor_member_id,
            actor_kind=actor_kind,
            request_id=request_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=details or {},
        )
        self._session.add(event)
        return event
