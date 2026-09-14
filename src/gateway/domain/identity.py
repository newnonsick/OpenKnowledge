from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import unicodedata


class PrincipalKind(StrEnum):
    SESSION = "session"
    API_KEY = "api_key"
    SERVICE = "service"
    COMPATIBILITY = "compatibility"
    SYSTEM = "system"


class SystemRole(StrEnum):
    SUPER_ADMIN = "super_admin"
    MEMBER = "member"


class SpaceRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    READER = "reader"


class MemberStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class APIKeyStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class PermissionProfile(StrEnum):
    READER = "reader"
    PROJECT_CONTRIBUTOR = "project_contributor"
    TRUSTED_MAINTAINER = "trusted_maintainer"
    IMPORT_WORKER = "import_worker"
    HUMAN_ADMIN = "human_admin"


def normalize_username(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip()).casefold()
    if not normalized:
        raise ValueError("Username is required")
    return normalized


@dataclass(frozen=True, slots=True)
class ServiceCredentialSpec:
    space_grants: frozenset[str]
    permission_profile: PermissionProfile
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.space_grants:
            raise ValueError("Service credentials require at least one space grant")
        if len(self.space_grants) > 100:
            raise ValueError("Too many space grants")
        for space_id in self.space_grants:
            cleaned = str(space_id).strip()
            if not cleaned or len(cleaned) > 64:
                raise ValueError("Invalid space grant")
        if self.permission_profile is PermissionProfile.HUMAN_ADMIN:
            raise ValueError("Service credentials must not use the human admin profile")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("Service credential expiry must be timezone aware")


def is_service_principal(principal: Principal) -> bool:
    return principal.kind is PrincipalKind.SERVICE


@dataclass(frozen=True, slots=True)
class Principal:
    subject_id: str
    kind: PrincipalKind
    system_role: SystemRole
    scopes: frozenset[str]
    credential_id: str | None = None
    active: bool = True
    restricted: bool = False
    space_grants: frozenset[str] | None = None
    permission_profile: PermissionProfile | None = None

    def __repr__(self) -> str:
        return (
            "Principal(subject_id={!r}, kind={!r}, system_role={!r}, scopes={!r}, "
            "credential_id=<redacted>, active={!r}, restricted={!r})"
        ).format(
            self.subject_id,
            self.kind,
            self.system_role,
            self.scopes,
            self.active,
            self.restricted,
        )
