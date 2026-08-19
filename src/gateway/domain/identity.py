from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import unicodedata


class PrincipalKind(StrEnum):
    SESSION = "session"
    API_KEY = "api_key"
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


def normalize_username(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip()).casefold()
    if not normalized:
        raise ValueError("Username is required")
    return normalized


@dataclass(frozen=True, slots=True)
class Principal:
    subject_id: str
    kind: PrincipalKind
    system_role: SystemRole
    scopes: frozenset[str]
    credential_id: str | None = None
    active: bool = True
    restricted: bool = False

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
