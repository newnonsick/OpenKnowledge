from uuid import uuid4

import pytest

from src.gateway.application.services.permission_service import (
    require_profile_operation,
    require_profile_route,
    require_profile_tool,
)
from src.gateway.domain.authorization import Action, intersect_key_grants, key_may_use_space
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import PermissionProfile, Principal, PrincipalKind, SystemRole
from src.gateway.domain.permission_profiles import (
    PROFILE_OPERATION_SCOPES,
    profile_allows_operation,
    profile_for_principal,
    profile_may_self_approve,
    profile_requires_session_principal,
    profile_requires_space_grants,
    profile_scopes,
)


def api_principal(profile=None, scopes=frozenset({"*"})) -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=scopes,
        credential_id=str(uuid4()),
        permission_profile=profile,
    )


def session_principal() -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
        credential_id=str(uuid4()),
    )


def test_key_grants_restrict_member_spaces_to_granted_subset() -> None:
    member_spaces = {"global", "space-a", "space-b"}
    assert intersect_key_grants(member_spaces, None) == member_spaces
    assert intersect_key_grants(member_spaces, frozenset({"space-a"})) == {"space-a"}
    assert intersect_key_grants(member_spaces, frozenset({"space-b", "global"})) == {"space-b", "global"}
    assert intersect_key_grants(member_spaces, frozenset({"unknown"})) == set()


def test_key_may_use_space_matches_grant_semantics() -> None:
    assert key_may_use_space(None, "space-a") is True
    assert key_may_use_space(frozenset({"space-a"}), "space-a") is True
    assert key_may_use_space(frozenset({"space-a"}), "space-b") is False
    assert key_may_use_space(frozenset(), "space-a") is False


def test_permission_profiles_cover_section_7_table() -> None:
    assert set(PROFILE_OPERATION_SCOPES) == {
        PermissionProfile.READER,
        PermissionProfile.PROJECT_CONTRIBUTOR,
        PermissionProfile.TRUSTED_MAINTAINER,
        PermissionProfile.IMPORT_WORKER,
        PermissionProfile.HUMAN_ADMIN,
    }
    assert profile_scopes(PermissionProfile.READER) == frozenset({"knowledge:read", "spaces:read", "chat:write"})
    assert "knowledge:write" in profile_scopes(PermissionProfile.PROJECT_CONTRIBUTOR)
    assert "spaces:members" in profile_scopes(PermissionProfile.TRUSTED_MAINTAINER)
    assert profile_requires_space_grants(PermissionProfile.HUMAN_ADMIN) is False
    assert profile_requires_space_grants(PermissionProfile.READER) is True


def test_routine_allow_flows_need_no_session() -> None:
    reader = api_principal(PermissionProfile.READER)
    require_profile_operation(reader, Action.CONTENT_READ)
    contributor = api_principal(PermissionProfile.PROJECT_CONTRIBUTOR)
    require_profile_operation(contributor, Action.CONTENT_WRITE)
    maintainer = api_principal(PermissionProfile.TRUSTED_MAINTAINER)
    require_profile_operation(maintainer, Action.CONTENT_WRITE)
    worker = api_principal(PermissionProfile.IMPORT_WORKER)
    require_profile_operation(worker, Action.CONTENT_WRITE)


def test_denied_operations_cannot_be_self_approved() -> None:
    for profile in (
        PermissionProfile.READER,
        PermissionProfile.PROJECT_CONTRIBUTOR,
        PermissionProfile.TRUSTED_MAINTAINER,
        PermissionProfile.IMPORT_WORKER,
        PermissionProfile.HUMAN_ADMIN,
    ):
        assert profile_may_self_approve(profile) is False
    reader = api_principal(PermissionProfile.READER)
    with pytest.raises(AuthorizationException):
        require_profile_operation(reader, Action.CONTENT_WRITE)
    contributor = api_principal(PermissionProfile.PROJECT_CONTRIBUTOR)
    with pytest.raises(AuthorizationException):
        require_profile_operation(contributor, Action.MEMBERSHIP_MANAGE)
    worker = api_principal(PermissionProfile.IMPORT_WORKER)
    with pytest.raises(AuthorizationException):
        require_profile_operation(worker, Action.SPACE_ARCHIVE)


def test_sensitive_control_operations_require_session_principal() -> None:
    assert profile_requires_session_principal(PermissionProfile.READER, Action.MEMBER_ADMIN) is True
    assert profile_requires_session_principal(PermissionProfile.TRUSTED_MAINTAINER, Action.MEMBERSHIP_MANAGE) is True
    assert profile_requires_session_principal(PermissionProfile.TRUSTED_MAINTAINER, Action.CONTENT_WRITE) is False
    maintainer_key = api_principal(PermissionProfile.TRUSTED_MAINTAINER)
    with pytest.raises(AuthorizationException):
        require_profile_operation(maintainer_key, Action.MEMBERSHIP_MANAGE)
    admin_session = session_principal()
    require_profile_operation(admin_session, Action.MEMBER_ADMIN)
    require_profile_operation(admin_session, Action.SPACE_ARCHIVE)


def test_unprofiled_principals_keep_existing_behavior() -> None:
    assert profile_for_principal(api_principal()) is None
    assert profile_for_principal(session_principal()) == PermissionProfile.HUMAN_ADMIN
    unprofiled = api_principal()
    require_profile_operation(unprofiled, Action.CONTENT_WRITE)
    require_profile_route(unprofiled, "knowledge.create")
    require_profile_tool(unprofiled, "knowledge.create.v1")


def test_profile_tool_and_route_parity_across_surfaces() -> None:
    reader = api_principal(PermissionProfile.READER)
    require_profile_tool(reader, "knowledge.search.v1")
    require_profile_route(reader, "knowledge.search")
    with pytest.raises(AuthorizationException):
        require_profile_tool(reader, "knowledge.create.v1")
    with pytest.raises(AuthorizationException):
        require_profile_route(reader, "knowledge.create")
    contributor = api_principal(PermissionProfile.PROJECT_CONTRIBUTOR)
    require_profile_tool(contributor, "knowledge.create.v1")
    require_profile_route(contributor, "knowledge.create")
    require_profile_tool(contributor, "sources.list.v1")
    require_profile_route(contributor, "source.list")
    require_profile_tool(contributor, "ingestion_jobs.list.v1")
    require_profile_route(contributor, "ingestion_job.list")
    with pytest.raises(AuthorizationException):
        require_profile_tool(contributor, "spaces.members.set.v1")
    with pytest.raises(AuthorizationException):
        require_profile_route(contributor, "space.members.set")
    maintainer = api_principal(PermissionProfile.TRUSTED_MAINTAINER)
    require_profile_tool(maintainer, "knowledge.archive.v1")
    require_profile_route(maintainer, "knowledge.delete")
    with pytest.raises(AuthorizationException):
        require_profile_tool(maintainer, "settings.propose.v1")
    with pytest.raises(AuthorizationException):
        require_profile_route(maintainer, "settings.mutate")
    with pytest.raises(AuthorizationException):
        require_profile_tool(maintainer, "unknown.tool.v1")


def test_profile_allows_operation_matches_section_7_matrix() -> None:
    assert profile_allows_operation(PermissionProfile.READER, Action.CONTENT_READ) is True
    assert profile_allows_operation(PermissionProfile.READER, Action.CONTENT_WRITE) is False
    assert profile_allows_operation(PermissionProfile.PROJECT_CONTRIBUTOR, Action.CONTENT_WRITE) is True
    assert profile_allows_operation(PermissionProfile.PROJECT_CONTRIBUTOR, Action.MEMBERSHIP_MANAGE) is False
    assert profile_allows_operation(PermissionProfile.TRUSTED_MAINTAINER, Action.MEMBERSHIP_MANAGE) is True
    assert profile_allows_operation(PermissionProfile.IMPORT_WORKER, Action.CONTENT_WRITE) is True
    assert profile_allows_operation(PermissionProfile.IMPORT_WORKER, Action.SPACE_ARCHIVE) is False
