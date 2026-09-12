from __future__ import annotations

from src.gateway.domain.authorization import Action
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal, PrincipalKind
from src.gateway.domain.permission_profiles import (
    PROFILE_SENSITIVE_OPERATIONS,
    profile_allows_operation,
    profile_for_principal,
    profile_requires_session_principal,
)


TOOL_ACTION: dict[str, Action] = {
    "spaces.list.v1": Action.SPACE_READ,
    "spaces.create.v1": Action.SPACE_CREATE,
    "spaces.archive.v1": Action.SPACE_ARCHIVE,
    "spaces.members.list.v1": Action.MEMBERSHIP_MANAGE,
    "spaces.members.set.v1": Action.MEMBERSHIP_MANAGE,
    "knowledge.search.v1": Action.CONTENT_READ,
    "knowledge.read.v1": Action.CONTENT_READ,
    "knowledge.create.v1": Action.CONTENT_WRITE,
    "knowledge.update.v1": Action.CONTENT_WRITE,
    "knowledge.archive.v1": Action.CONTENT_WRITE,
    "sources.list.v1": Action.CONTENT_READ,
    "ingestion_jobs.list.v1": Action.CONTENT_READ,
    "ingestion_jobs.cancel.v1": Action.CONTENT_WRITE,
    "ingestion_jobs.retry.v1": Action.CONTENT_WRITE,
    "retrieval.explain.v1": Action.CONTENT_READ,
    "settings.inspect.v1": Action.SPACE_READ,
    "settings.propose.v1": Action.MEMBER_ADMIN,
}

ROUTE_ACTION: dict[str, Action] = {
    "space.list": Action.SPACE_READ,
    "space.create": Action.SPACE_CREATE,
    "space.archive": Action.SPACE_ARCHIVE,
    "space.members.list": Action.MEMBERSHIP_MANAGE,
    "space.members.set": Action.MEMBERSHIP_MANAGE,
    "knowledge.search": Action.CONTENT_READ,
    "knowledge.read": Action.CONTENT_READ,
    "knowledge.create": Action.CONTENT_WRITE,
    "knowledge.update": Action.CONTENT_WRITE,
    "knowledge.delete": Action.CONTENT_WRITE,
    "source.list": Action.CONTENT_READ,
    "source.upload": Action.CONTENT_WRITE,
    "ingestion_job.list": Action.CONTENT_READ,
    "ingestion_job.mutate": Action.CONTENT_WRITE,
    "settings.inspect": Action.SPACE_READ,
    "settings.mutate": Action.MEMBER_ADMIN,
    "api_key.manage": Action.API_KEY_MANAGE,
    "session.manage": Action.SESSION_MANAGE,
    "member.admin": Action.MEMBER_ADMIN,
}


def require_profile_operation(principal: Principal, action: Action) -> None:
    profile = profile_for_principal(principal)
    if profile is None:
        return
    if profile_requires_session_principal(profile, action):
        if principal.kind is not PrincipalKind.SESSION:
            raise AuthorizationException()
        return
    if not profile_allows_operation(profile, action):
        raise AuthorizationException()


def require_profile_tool(principal: Principal, tool_name: str) -> None:
    action = TOOL_ACTION.get(tool_name)
    if action is None:
        raise AuthorizationException()
    require_profile_operation(principal, action)


def require_profile_route(principal: Principal, route: str) -> None:
    action = ROUTE_ACTION.get(route)
    if action is None:
        return
    require_profile_operation(principal, action)


def is_sensitive_operation(action: Action) -> bool:
    return action in PROFILE_SENSITIVE_OPERATIONS
