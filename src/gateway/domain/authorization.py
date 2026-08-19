from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from src.gateway.domain.identity import Principal, SpaceRole, SystemRole


class Action(StrEnum):
    PASSWORD_CHANGE = "password:change"
    MFA_ENROLL = "mfa:enroll"
    MEMBER_ADMIN = "member:admin"
    SPACE_CREATE = "space:create"
    SPACE_READ = "space:read"
    SPACE_UPDATE = "space:update"
    SPACE_ARCHIVE = "space:archive"
    MEMBERSHIP_MANAGE = "membership:manage"
    CONTENT_READ = "content:read"
    CONTENT_WRITE = "content:write"
    API_KEY_MANAGE = "api_key:manage"
    SESSION_MANAGE = "session:manage"


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    principal: Principal
    space_role: SpaceRole | None = None


_RESTRICTED_ACTIONS = frozenset({Action.PASSWORD_CHANGE, Action.MFA_ENROLL})
_SUPER_ADMIN_ACTIONS = frozenset({Action.MEMBER_ADMIN})
_SPACE_ACTIONS = {
    SpaceRole.READER: frozenset({Action.SPACE_READ, Action.CONTENT_READ}),
    SpaceRole.EDITOR: frozenset(
        {Action.SPACE_READ, Action.SPACE_UPDATE, Action.CONTENT_READ, Action.CONTENT_WRITE}
    ),
    SpaceRole.OWNER: frozenset(
        {
            Action.SPACE_READ,
            Action.SPACE_UPDATE,
            Action.SPACE_ARCHIVE,
            Action.MEMBERSHIP_MANAGE,
            Action.CONTENT_READ,
            Action.CONTENT_WRITE,
        }
    ),
}
_SCOPE_BY_ACTION = {
    Action.CONTENT_READ: "knowledge:read",
    Action.CONTENT_WRITE: "knowledge:write",
    Action.SPACE_READ: "spaces:read",
    Action.SPACE_CREATE: "spaces:write",
    Action.SPACE_UPDATE: "spaces:write",
    Action.SPACE_ARCHIVE: "spaces:write",
    Action.MEMBERSHIP_MANAGE: "spaces:members",
    Action.API_KEY_MANAGE: "api_keys:write",
    Action.SESSION_MANAGE: "sessions:write",
    Action.MEMBER_ADMIN: "members:write",
}


def is_allowed(context: AuthorizationContext, action: Action) -> bool:
    principal = context.principal
    if not principal.active:
        return False
    if principal.restricted:
        return action in _RESTRICTED_ACTIONS
    scope = _SCOPE_BY_ACTION.get(action)
    if scope and "*" not in principal.scopes and scope not in principal.scopes:
        return False
    if action in {Action.PASSWORD_CHANGE, Action.MFA_ENROLL, Action.API_KEY_MANAGE, Action.SESSION_MANAGE}:
        return True
    if action is Action.SPACE_CREATE:
        return True
    if action in _SUPER_ADMIN_ACTIONS:
        return principal.system_role is SystemRole.SUPER_ADMIN
    if context.space_role is None:
        return False
    return action in _SPACE_ACTIONS[context.space_role]
