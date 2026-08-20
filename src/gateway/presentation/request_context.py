from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID, uuid4

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.gateway.observability import create_traceparent, trace_id_context, traceparent_context


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
        trace_id, traceparent = create_traceparent(request.headers.get("traceparent"))
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        request_token = request_id_context.set(request_id)
        trace_token = trace_id_context.set(trace_id)
        traceparent_token = traceparent_context.set(traceparent)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            response.headers["traceparent"] = traceparent
            return response
        finally:
            traceparent_context.reset(traceparent_token)
            trace_id_context.reset(trace_token)
            request_id_context.reset(request_token)
