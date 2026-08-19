from __future__ import annotations

from contextvars import ContextVar, Token
from uuid import UUID

from sqlalchemy import text

from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal


_request_principal: ContextVar[Principal | None] = ContextVar(
    "gateway_request_principal",
    default=None,
)


def bind_principal(principal: Principal) -> Token:
    return _request_principal.set(principal)


def reset_principal(token: Token) -> None:
    _request_principal.reset(token)


def get_bound_principal() -> Principal | None:
    return _request_principal.get()


async def set_principal_context(connection, principal: Principal) -> None:
    if not principal.active:
        raise AuthorizationException()
    try:
        member_id = str(UUID(principal.subject_id))
    except ValueError as exc:
        raise AuthorizationException() from exc
    await connection.execute(
        text(
            "SELECT set_config('app.principal_id', :member_id, true), "
            "set_config('app.principal_restricted', :restricted, true)"
        ),
        {
            "member_id": member_id,
            "restricted": "true" if principal.restricted else "false",
        },
    )


async def current_principal_id(connection) -> UUID | None:
    value = await connection.scalar(
        text("SELECT NULLIF(current_setting('app.principal_id', true), '')")
    )
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None
