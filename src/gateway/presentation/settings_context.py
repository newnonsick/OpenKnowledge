from __future__ import annotations

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.gateway.config import reset_runtime_settings, set_runtime_settings


class SettingsContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        token = set_runtime_settings(request.app.state.settings)
        try:
            return await call_next(request)
        finally:
            reset_runtime_settings(token)
