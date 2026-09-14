from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services import oidc_service
from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.services.quota_service import QuotaGuard, quota_service_from_settings
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import AuthenticationException, AuthorizationException, GatewayException, QuotaExceededException
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.database import get_session_factory
from src.gateway.presentation.request_context import normalized_request_id


_OIDC_ENDPOINT_CACHE: dict[str, oidc_service.OidcEndpoints] = {}


class MCPRequestAuthenticator:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        codec: APIKeyCodec | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._codec = codec

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is not None:
            return self._session_factory
        return get_session_factory()

    def _codec_or_default(self) -> APIKeyCodec:
        if self._codec is not None:
            return self._codec
        gateway = get_settings().gateway
        if not gateway.api_key_peppers:
            raise AuthenticationException("Invalid API key provided.")
        return APIKeyCodec(
            {version: SecretValue(value) for version, value in gateway.api_key_peppers.items()},
            active_pepper_version=gateway.active_api_key_pepper_version,
        )

    @staticmethod
    def _bearer_token(headers: Mapping[str, str]) -> str | None:
        lowered = {str(key).lower(): value for key, value in headers.items()}
        authorization = lowered.get("authorization")
        if authorization:
            parts = authorization.strip().split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()
                if token and "\x00" not in token:
                    return token
        api_key = lowered.get("x-api-key")
        if api_key:
            token = api_key.strip()
            if token and "\x00" not in token:
                return token
        return None

    async def authenticate(self, headers: Mapping[str, str]) -> Principal:
        token = self._bearer_token(headers)
        if token is None:
            raise AuthenticationException("Missing or malformed authentication credentials.")
        try:
            factory = self._factory()
            async with factory.begin() as session:
                return await APIKeyService(session, self._codec_or_default()).resolve(token)
        except AuthenticationException:
            pass
        return await self._authenticate_oidc(token, headers)

    async def _authenticate_oidc(self, token: str, headers: Mapping[str, str]) -> Principal:
        gateway = get_settings().gateway
        if not gateway.oidc_enabled:
            raise AuthenticationException("Invalid API key provided.")
        issuer = (gateway.oidc_issuer or "").rstrip("/")
        if not issuer or not gateway.oidc_client_id:
            raise AuthenticationException("Invalid API key provided.")
        endpoints = await _oidc_endpoints(issuer)
        try:
            jwks = await oidc_service.fetch_jwks(endpoints.jwks_uri)
        except GatewayException as exc:
            raise AuthenticationException("Invalid API key provided.") from exc
        factory = self._factory()
        async with factory.begin() as session:
            try:
                tokens, result = await oidc_service.authenticate_callback(
                    session,
                    id_token=token,
                    jwks=jwks,
                    request_id=self.request_id(headers),
                    jwks_uri=endpoints.jwks_uri,
                )
            except AuthenticationException:
                raise
            except GatewayException as exc:
                raise AuthenticationException("Invalid API key provided.") from exc
        principal = oidc_service.build_principal(result.member)
        scopes = _oidc_token_scopes(tokens.claims)
        if scopes:
            principal = replace(principal, scopes=frozenset(scopes))
        return principal

    @staticmethod
    def request_id(headers: Mapping[str, str]) -> str:
        lowered = {str(key).lower(): value for key, value in headers.items()}
        return normalized_request_id(lowered.get("x-request-id"))

    @staticmethod
    def idempotency_key(headers: Mapping[str, str]) -> str:
        lowered = {str(key).lower(): value for key, value in headers.items()}
        candidate = (lowered.get("idempotency-key") or "").strip()
        if not candidate or len(candidate) > 128:
            return str(uuid4())
        return candidate


async def _oidc_endpoints(issuer: str) -> oidc_service.OidcEndpoints:
    cached = _OIDC_ENDPOINT_CACHE.get(issuer)
    if cached is not None:
        return cached
    try:
        endpoints = await oidc_service.discover(issuer)
    except GatewayException as exc:
        raise AuthenticationException("Invalid API key provided.") from exc
    _OIDC_ENDPOINT_CACHE[issuer] = endpoints
    return endpoints


def clear_oidc_endpoint_cache() -> None:
    _OIDC_ENDPOINT_CACHE.clear()


def _oidc_token_scopes(claims: Mapping[str, Any]) -> frozenset[str]:
    raw = claims.get("scope", claims.get("scp", ()))
    if isinstance(raw, str):
        items: list[Any] = raw.split()
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    return frozenset({str(item).strip() for item in items if str(item).strip()})


async def require_mcp_scope(principal: Principal, scope: str) -> Principal:
    if principal is None or not principal.active or principal.restricted:
        raise AuthorizationException()
    if "*" not in principal.scopes and scope not in principal.scopes:
        raise AuthorizationException()
    return principal


def mcp_quota_guard(principal: Principal, space_id: str | None) -> QuotaGuard:
    service = quota_service_from_settings(get_settings().gateway)
    return QuotaGuard(service=service, principal=principal, space_id=space_id)


def quota_error_details(exc: QuotaExceededException) -> dict[str, object]:
    retry_after = int(exc.details.get("retry_after_seconds", 1))
    return {"retry_after_seconds": max(1, retry_after)}
