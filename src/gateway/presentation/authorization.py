from __future__ import annotations

from fastapi import Request

from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal


async def require_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None or not principal.active or principal.restricted:
        raise AuthorizationException()
    return principal


def require_scope(scope: str):
    async def dependency(request: Request) -> Principal:
        principal = getattr(request.state, "principal", None)
        if (
            principal is None
            or not principal.active
            or principal.restricted
            or ("*" not in principal.scopes and scope not in principal.scopes)
        ):
            raise AuthorizationException()
        return principal

    return dependency
