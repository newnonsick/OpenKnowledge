from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.identity import Principal, PrincipalKind, SpaceRole, SystemRole


def principal(*, system_role: SystemRole = SystemRole.MEMBER, scopes: frozenset[str] = frozenset({"*"})) -> Principal:
    return Principal(
        subject_id="member-1",
        kind=PrincipalKind.API_KEY,
        system_role=system_role,
        scopes=scopes,
    )


def test_space_role_decisions_are_centralized() -> None:
    reader = AuthorizationContext(principal=principal(), space_role=SpaceRole.READER)
    editor = AuthorizationContext(principal=principal(), space_role=SpaceRole.EDITOR)
    owner = AuthorizationContext(principal=principal(), space_role=SpaceRole.OWNER)
    assert is_allowed(reader, Action.SPACE_READ) is True
    assert is_allowed(reader, Action.CONTENT_WRITE) is False
    assert is_allowed(editor, Action.CONTENT_WRITE) is True
    assert is_allowed(editor, Action.MEMBERSHIP_MANAGE) is False
    assert is_allowed(owner, Action.MEMBERSHIP_MANAGE) is True


def test_super_admin_does_not_implicitly_gain_content_access() -> None:
    context = AuthorizationContext(
        principal=principal(system_role=SystemRole.SUPER_ADMIN),
        space_role=None,
    )
    assert is_allowed(context, Action.MEMBER_ADMIN) is True
    assert is_allowed(context, Action.SPACE_READ) is False
    assert is_allowed(context, Action.CONTENT_READ) is False


def test_api_key_scopes_can_only_reduce_member_authority() -> None:
    scoped = AuthorizationContext(
        principal=principal(scopes=frozenset({"knowledge:read"})),
        space_role=SpaceRole.EDITOR,
    )
    assert is_allowed(scoped, Action.CONTENT_READ) is True
    assert is_allowed(scoped, Action.CONTENT_WRITE) is False


def test_disabled_and_restricted_principals_fail_closed() -> None:
    disabled = Principal(
        subject_id="member-1",
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset(),
        active=False,
    )
    restricted = Principal(
        subject_id="member-1",
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset(),
        restricted=True,
    )
    assert is_allowed(AuthorizationContext(disabled, SpaceRole.OWNER), Action.SPACE_READ) is False
    assert is_allowed(AuthorizationContext(restricted, SpaceRole.OWNER), Action.CONTENT_READ) is False
    assert is_allowed(AuthorizationContext(restricted, None), Action.PASSWORD_CHANGE) is True
