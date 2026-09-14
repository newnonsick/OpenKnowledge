from __future__ import annotations

import json
import logging

from fastapi import Request

from src.gateway.application.services.quota_service import quota_service_from_settings
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import QuotaExceededException
from src.gateway.presentation.auth import is_public_path
from src.gateway.presentation.errors import protocol_error_response


logger = logging.getLogger(__name__)

_BODY_SPACE_KEYS = ("space_id", "workspace_id", "active_space_id")
_BODY_SNIFF_LIMIT = 65536


async def _check_persistent_window(service, principal, space_id: str | None) -> None:
    if space_id is None:
        return
    try:
        from src.gateway.infrastructure.database import get_session_factory

        factory = get_session_factory()
        async with factory() as session:
            await service.check_persistent_window(session, principal, space_id=space_id)
    except QuotaExceededException:
        raise
    except Exception:
        logger.debug("Persistent quota check unavailable; burst guard still applies")


def extract_quota_space(query: dict, body: dict) -> str | None:
    for key in ("space_id", "active_space_id", "workspace_id"):
        value = query.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("space_ids",):
        value = query.get(key)
        if isinstance(value, str) and value.strip():
            first = value.split(",")[0].strip()
            if first:
                return first
    for key in _BODY_SPACE_KEYS:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    space_ids = body.get("space_ids")
    if isinstance(space_ids, list) and space_ids:
        first = space_ids[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


class QuotaMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        if is_public_path(request.url.path):
            await self.app(scope, receive, send)
            return
        service = quota_service_from_settings(get_settings().gateway)
        space_id, replay = await self._request_space(request, receive)
        principal = self._outer_principal(scope)
        if principal is None or not principal.active:
            await self.app(scope, replay, send)
            return
        try:
            await service.acquire(principal, space_id=space_id)
            await _check_persistent_window(service, principal, space_id)
        except QuotaExceededException as exc:
            retry_after = int(exc.details.get("retry_after_seconds", 1))
            try:
                request.state.retry_after_seconds = retry_after
            except (AttributeError, KeyError):
                pass
            logger.warning(
                "Quota exceeded",
                extra={"quota": exc.quota, "space_id": space_id},
            )
            response = protocol_error_response(
                request,
                exc.status_code,
                exc.error_type,
                exc.code,
                exc.message,
                details={"retry_after_seconds": retry_after},
            )
            response.headers["Retry-After"] = str(retry_after)
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, replay, send)
        finally:
            await service.release(principal, space_id=space_id)

    @staticmethod
    def _outer_principal(scope):
        state = scope.get("state")
        if isinstance(state, dict):
            principal = state.get("principal")
            if principal is not None:
                return principal
            return None
        if state is None:
            return None
        try:
            return state.principal
        except (AttributeError, KeyError):
            return None

    async def _request_space(self, request: Request, receive):
        try:
            query = dict(request.query_params)
        except Exception:
            query = {}
        body: dict = {}
        sniffed = b""
        messages = []
        disconnected = False
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                disconnected = True
                break
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(sniffed) < _BODY_SNIFF_LIMIT:
                sniffed += chunk[: _BODY_SNIFF_LIMIT - len(sniffed)]
            if not message.get("more_body", False):
                break
        index = 0

        async def replay():
            nonlocal index
            if index >= len(messages):
                return await receive()
            message = messages[index]
            index += 1
            return message

        if not disconnected and request.headers.get("Content-Type", "").startswith("application/json"):
            try:
                parsed = json.loads(sniffed) if sniffed else None
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:
                body = {}
        try:
            return extract_quota_space(query, body), replay
        except Exception:
            return None, replay
