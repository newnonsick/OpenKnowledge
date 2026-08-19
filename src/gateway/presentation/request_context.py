from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID, uuid4

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def normalized_request_id(value: str | None) -> str:
    if value:
        try:
            parsed = UUID(value)
            if str(parsed) == value.lower():
                return str(parsed)
        except (ValueError, AttributeError):
            pass
    return str(uuid4())


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid4()))


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = normalized_request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        token = request_id_context.set(request_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_context.reset(token)
