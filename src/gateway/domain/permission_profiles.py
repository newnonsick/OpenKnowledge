from __future__ import annotations

from src.gateway.domain.authorization import Action
from src.gateway.domain.identity import PermissionProfile, Principal, PrincipalKind


PROFILE_OPERATION_SCOPES: dict[PermissionProfile, frozenset[str]] = {
    PermissionProfile.READER: frozenset({"knowledge:read", "spaces:read", "chat:write"}),
    PermissionProfile.PROJECT_CONTRIBUTOR: frozenset(
        {"knowledge:read", "knowledge:write", "spaces:read", "chat:write"}
    ),
    PermissionProfile.TRUSTED_MAINTAINER: frozenset(
        {
            "knowledge:read",
            "knowledge:write",
            "spaces:read",
            "spaces:write",
            "spaces:members",
            "chat:write",
        }
    ),
    PermissionProfile.IMPORT_WORKER: frozenset({"knowledge:read", "knowledge:write", "spaces:read", "chat:write"}),
    PermissionProfile.HUMAN_ADMIN: frozenset({"*"}),
}

PROFILE_GRANT_REQUIREMENTS: dict[PermissionProfile, bool] = {
    PermissionProfile.READER: True,
    PermissionProfile.PROJECT_CONTRIBUTOR: True,
    PermissionProfile.TRUSTED_MAINTAINER: True,
    PermissionProfile.IMPORT_WORKER: True,
    PermissionProfile.HUMAN_ADMIN: False,
}

PROFILE_SENSITIVE_OPERATIONS: frozenset[Action] = frozenset(
    {
        Action.MEMBER_ADMIN,
        Action.MEMBERSHIP_MANAGE,
        Action.SPACE_ARCHIVE,
        Action.API_KEY_MANAGE,
        Action.SESSION_MANAGE,
    }
)

PROFILE_ROUTINE_OPERATIONS: dict[PermissionProfile, frozenset[Action]] = {
    PermissionProfile.READER: frozenset({Action.CONTENT_READ, Action.SPACE_READ}),
    PermissionProfile.PROJECT_CONTRIBUTOR: frozenset(
        {Action.CONTENT_READ, Action.CONTENT_WRITE, Action.SPACE_READ}
    ),
    PermissionProfile.TRUSTED_MAINTAINER: frozenset(
        {
            Action.CONTENT_READ,
            Action.CONTENT_WRITE,
            Action.SPACE_READ,
            Action.SPACE_UPDATE,
            Action.SPACE_CREATE,
            Action.SPACE_ARCHIVE,
            Action.MEMBERSHIP_MANAGE,
        }
    ),
    PermissionProfile.IMPORT_WORKER: frozenset({Action.CONTENT_READ, Action.CONTENT_WRITE, Action.SPACE_READ}),
    PermissionProfile.HUMAN_ADMIN: frozenset(set(Action)),
}


def profile_for_principal(principal: Principal) -> PermissionProfile | None:
    if principal.permission_profile is not None:
        return principal.permission_profile
    if principal.kind is PrincipalKind.SESSION:
        return PermissionProfile.HUMAN_ADMIN
    return None


def profile_may_self_approve(profile: PermissionProfile) -> bool:
    return False


def profile_requires_session_principal(profile: PermissionProfile, action: Action) -> bool:
    if profile is PermissionProfile.HUMAN_ADMIN:
        return True
    return action in PROFILE_SENSITIVE_OPERATIONS


def profile_allows_operation(profile: PermissionProfile, action: Action) -> bool:
    routine = PROFILE_ROUTINE_OPERATIONS.get(profile)
    if routine is None:
        return False
    return action in routine


def profile_scopes(profile: PermissionProfile) -> frozenset[str]:
    return PROFILE_OPERATION_SCOPES[profile]


def profile_requires_space_grants(profile: PermissionProfile) -> bool:
    return PROFILE_GRANT_REQUIREMENTS[profile]
