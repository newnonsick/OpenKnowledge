

from __future__ import annotations

import hmac
import logging
from typing import Callable, Dict, List, Optional, Sequence, Set
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from src.gateway.config import get_settings, settings
from src.gateway.domain.exceptions import AuthenticationException

logger = logging.getLogger(__name__)

PUBLIC_PATHS: Set[str] = {
    "/health",
    "/v1/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
}

def is_public_path(path: str) -> bool:

    normalized = path.rstrip("/") if path != "/" else path
    if normalized == "":
        normalized = "/"
    for pub in PUBLIC_PATHS:
        if normalized == pub or path.startswith(pub + "/"):
            return True
    return False

def authenticate_credentials(
    auth_header: Optional[str] = None,
    x_api_key: Optional[str] = None,
    allowed_keys: Optional[Sequence[str]] = None,
) -> str:

    valid_keys = (
        allowed_keys
        if allowed_keys is not None
        else get_settings().gateway.gateway_api_keys
    )
    if isinstance(valid_keys, str):
        valid_keys = [valid_keys]

    candidate_key: Optional[str] = None

    if auth_header:
        parts = auth_header.strip().split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
            if token and "\x00" not in token:
                candidate_key = token

    if not candidate_key and x_api_key:
        token = x_api_key.strip()
        if token and "\x00" not in token:
            candidate_key = token

    if not candidate_key:
        raise AuthenticationException("Missing or malformed authentication credentials.")

    candidate_bytes = candidate_key.encode("utf-8")
    for key in valid_keys:
        if hmac.compare_digest(candidate_bytes, key.encode("utf-8")):
            return candidate_key

    raise AuthenticationException("Invalid API key provided.")

def authenticate_request(
    headers: Dict[str, str],
    allowed_keys: Optional[Sequence[str]] = None,
) -> str:

    auth_header = headers.get("authorization") or headers.get("Authorization")
    api_key_header = (
        headers.get("x-api-key")
        or headers.get("X-Api-Key")
        or headers.get("X-API-KEY")
    )
    return authenticate_credentials(
        auth_header=auth_header,
        x_api_key=api_key_header,
        allowed_keys=allowed_keys,
    )

class AuthValidator:

    def __init__(self, allowed_keys: Optional[Sequence[str]] = None):
        self._allowed_keys = list(allowed_keys) if allowed_keys is not None else None

    @property
    def allowed_keys(self) -> List[str]:
        if self._allowed_keys is not None:
            return self._allowed_keys
        return list(get_settings().gateway.gateway_api_keys)

    def validate(
        self,
        auth_header: Optional[str] = None,
        x_api_key: Optional[str] = None,
    ) -> bool:

        authenticate_credentials(
            auth_header=auth_header,
            x_api_key=x_api_key,
            allowed_keys=self.allowed_keys,
        )
        return True

class APIKeyAuthMiddleware(BaseHTTPMiddleware):

    def __init__(self, app, allowed_keys: Optional[Sequence[str]] = None):
        super().__init__(app)
        self._allowed_keys = list(allowed_keys) if allowed_keys is not None else None

    @property
    def allowed_keys(self) -> List[str]:
        if self._allowed_keys is not None:
            return self._allowed_keys
        return list(get_settings().gateway.gateway_api_keys)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        if is_public_path(path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
        api_key_header = (
            request.headers.get("x-api-key")
            or request.headers.get("X-Api-Key")
            or request.headers.get("X-API-KEY")
        )

        try:
            matched_key = authenticate_credentials(
                auth_header=auth_header,
                x_api_key=api_key_header,
                allowed_keys=self.allowed_keys,
            )

            request.state.api_key = matched_key
        except AuthenticationException as exc:
            logger.warning(f"Unauthorized access attempt to {path}: {exc.message}")
            return self._build_401_response(path, exc)

        return await call_next(request)

    def _build_401_response(self, path: str, exc: AuthenticationException) -> JSONResponse:

        if path.startswith("/v1/messages"):

            return JSONResponse(
                status_code=401,
                content={
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": exc.message,
                    },
                },
            )
        else:

            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": exc.message,
                        "type": exc.error_type,
                        "code": exc.code,
                    }
                },
            )
