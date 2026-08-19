from uuid import uuid4

import pytest
from starlette.requests import Request

from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.presentation.authorization import require_scope


def request_with_scopes(scopes: frozenset[str]) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/protected",
            "headers": [],
        }
    )
    request.state.principal = Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=scopes,
    )
    return request


async def test_api_key_scope_can_reduce_route_access() -> None:
    dependency = require_scope("knowledge:write")

    with pytest.raises(AuthorizationException):
        await dependency(request_with_scopes(frozenset({"knowledge:read"})))

    principal = await dependency(
        request_with_scopes(frozenset({"knowledge:write"}))
    )

    assert principal.scopes == frozenset({"knowledge:write"})
