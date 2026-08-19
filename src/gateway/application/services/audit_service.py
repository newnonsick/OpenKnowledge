from __future__ import annotations

from uuid import UUID

from src.gateway.application.security.tokens import SecretValue
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel


_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "credential",
    "password",
    "recovery",
    "secret",
    "token",
    "totp",
)


def sanitize_audit_details(value):
    if isinstance(value, SecretValue):
        return None
    if isinstance(value, dict):
        return {
            key: sanitized
            for key, child in value.items()
            if not any(fragment in str(key).casefold() for fragment in _SENSITIVE_KEY_FRAGMENTS)
            if (sanitized := sanitize_audit_details(child)) is not None
        }
    if isinstance(value, (list, tuple)):
        return [sanitized for child in value if (sanitized := sanitize_audit_details(child)) is not None]
    return value


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    def record(
        self,
        *,
        actor_member_id: UUID | None,
        actor_kind: str,
        request_id: str,
        action: str,
        resource_type: str,
        resource_id: str | None,
        outcome: str = "success",
        details: dict | None = None,
    ) -> AuditEventModel:
        safe_details = sanitize_audit_details(details or {})
        return self._repository.append(
            actor_member_id=actor_member_id,
            actor_kind=actor_kind,
            request_id=request_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=safe_details,
        )
