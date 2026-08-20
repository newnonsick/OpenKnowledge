

from __future__ import annotations

import hmac
import hashlib
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Set
from uuid import UUID
from urllib.parse import urlparse
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from src.gateway.config import get_settings
from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.services.session_service import SessionService
from src.gateway.domain.exceptions import AuthenticationException, CSRFException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.database import get_session_factory
from src.gateway.infrastructure.persistence.identity_models import CompatibilityPrincipalModel, MemberModel, SessionCredentialModel
from src.gateway.infrastructure.persistence.principal_context import bind_principal, reset_principal
from src.gateway.presentation.errors import protocol_error_response

logger = logging.getLogger(__name__)

PUBLIC_PATHS: Set[str] = {
    "/health",
    "/healthz/live",
    "/healthz/ready",
    "/v1/health",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
    "/metrics",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
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

class APIKeyAuthMiddleware:

    def __init__(
        self,
        app,
        allowed_keys: Optional[Sequence[str]] = None,
        api_key_peppers: Optional[dict[int, str]] = None,
        active_api_key_pepper_version: int = 1,
        legacy_api_keys_enabled: bool = True,
        require_persisted_legacy_principals: bool = False,
        session_factory=None,
    ):
        self.app = app
        self._allowed_keys = list(allowed_keys) if allowed_keys is not None else None
        self._session_factory = session_factory
        self._legacy_api_keys_enabled = legacy_api_keys_enabled
        self._require_persisted_legacy_principals = (
            require_persisted_legacy_principals
        )
        self._api_key_codec = (
            APIKeyCodec(
                {
                    version: SecretValue(pepper)
                    for version, pepper in api_key_peppers.items()
                },
                active_pepper_version=active_api_key_pepper_version,
            )
            if api_key_peppers
            else None
        )

    @property
    def allowed_keys(self) -> List[str]:
        if self._allowed_keys is not None:
            return self._allowed_keys
        return list(get_settings().gateway.gateway_api_keys)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        path = request.url.path

        if is_public_path(path):
            await self.app(scope, receive, send)
            return

        try:
            principal = await self._resolve_principal(request)
            request.state.principal = principal
        except AuthenticationException as exc:
            logger.warning("Unauthorized request", extra={"path": path})
            response = self._build_401_response(request, exc)
            await response(scope, receive, send)
            return
        except CSRFException as exc:
            logger.warning("Request verification failed", extra={"path": path})
            response = self._build_error_response(request, exc)
            await response(scope, receive, send)
            return

        token = bind_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(token)

    async def _resolve_principal(self, request: Request) -> Principal:
        auth_header = request.headers.get("Authorization") or request.headers.get(
            "authorization"
        )
        bearer_token = self._bearer_token(auth_header)
        if bearer_token and bearer_token.startswith("aigw_v"):
            if self._api_key_codec is None:
                raise AuthenticationException("Invalid API key provided.")
            factory = self._session_factory or get_session_factory()
            async with factory.begin() as session:
                return await APIKeyService(session, self._api_key_codec).resolve(
                    bearer_token
                )
        if bearer_token is None:
            access_token = request.cookies.get("__Host-aigw-access")
            if access_token:
                factory = self._session_factory or get_session_factory()
                async with factory.begin() as session:
                    service = SessionService(session)
                    principal = await service.resolve_access(access_token)
                    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                        await self._verify_cookie_request(
                            request,
                            service,
                            session,
                            principal,
                        )
                    return principal
        if not self._legacy_api_keys_enabled:
            raise AuthenticationException("Invalid API key provided.")
        api_key_header = (
            request.headers.get("x-api-key")
            or request.headers.get("X-Api-Key")
            or request.headers.get("X-API-KEY")
        )
        matched_key = authenticate_credentials(
            auth_header=auth_header,
            x_api_key=api_key_header,
            allowed_keys=self.allowed_keys,
        )
        digest = hashlib.sha256(matched_key.encode("utf-8")).digest()
        if self._require_persisted_legacy_principals:
            factory = self._session_factory or get_session_factory()
            async with factory.begin() as session:
                record = await session.scalar(
                    select(CompatibilityPrincipalModel).where(
                        CompatibilityPrincipalModel.key_digest == digest.hex()
                    )
                )
                current_time = datetime.now(timezone.utc)
                if (
                    record is None
                    or record.revoked_at is not None
                    or current_time >= record.expires_at
                ):
                    raise AuthenticationException("Invalid API key provided.")
                member = await session.get(MemberModel, record.id)
                if member is None or member.status != "active":
                    raise AuthenticationException("Invalid API key provided.")
                return Principal(
                    subject_id=str(record.id),
                    kind=PrincipalKind.COMPATIBILITY,
                    system_role=SystemRole(member.system_role),
                    scopes=frozenset(
                        {
                            "chat:write",
                            "knowledge:read",
                            "knowledge:write",
                        }
                    ),
                    credential_id=str(record.id),
                )
        return Principal(
            subject_id=str(UUID(bytes=digest[:16])),
            kind=PrincipalKind.COMPATIBILITY,
            system_role=SystemRole.MEMBER,
            scopes=frozenset({"*"}),
        )

    async def _verify_cookie_request(
        self,
        request: Request,
        service: SessionService,
        session,
        principal: Principal,
    ) -> None:
        candidate = request.headers.get("X-CSRF-Token")
        if not candidate or principal.credential_id is None:
            raise CSRFException()
        try:
            credential_id = UUID(principal.credential_id)
        except ValueError as exc:
            raise CSRFException() from exc
        credential = await session.get(SessionCredentialModel, credential_id)
        if credential is None or not await service.verify_csrf(
            credential.family_id,
            candidate,
        ):
            raise CSRFException()
        origin = request.headers.get("Origin")
        public_base_url = get_settings().gateway.public_base_url
        expected_url = public_base_url or str(request.base_url)
        parsed = urlparse(expected_url)
        expected_origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin != expected_origin:
            raise CSRFException()
        content_type = request.headers.get("Content-Type", "")
        content_length = request.headers.get("Content-Length")
        if content_length not in {None, "0"} and not content_type.startswith(
            (
                "application/json",
                "application/x-www-form-urlencoded",
                "multipart/form-data",
            )
        ):
            raise CSRFException()

    @staticmethod
    def _bearer_token(auth_header: Optional[str]) -> Optional[str]:
        if not auth_header:
            return None
        parts = auth_header.strip().split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        token = parts[1].strip()
        if not token or "\x00" in token:
            return None
        return token

    def _build_401_response(self, request: Request, exc: AuthenticationException) -> JSONResponse:
        return protocol_error_response(
            request,
            exc.status_code,
            exc.error_type,
            exc.code,
            exc.message,
        )

    def _build_error_response(self, request: Request, exc: CSRFException) -> JSONResponse:
        return protocol_error_response(
            request,
            exc.status_code,
            exc.error_type,
            exc.code,
            exc.message,
        )
