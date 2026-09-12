from __future__ import annotations

import logging

from src.gateway.application.services.quota_service import quota_service_from_settings
from src.gateway.config import get_settings
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.database import get_session_factory


logger = logging.getLogger(__name__)


async def record_token_usage(
    principal: Principal | None,
    *,
    space_id: str | None,
    tokens: int,
) -> None:
    if principal is None or not principal.active or tokens <= 0:
        return
    try:
        service = quota_service_from_settings(get_settings().gateway)
        factory = get_session_factory()
        async with factory.begin() as session:
            await service.record_usage(
                session,
                principal,
                space_id=space_id or "global",
                tokens=tokens,
            )
    except Exception as exc:
        logger.debug("Quota usage recording skipped", extra={"exception_class": type(exc).__name__})


async def record_storage_usage(
    principal: Principal | None,
    *,
    space_id: str | None,
    storage_bytes: int,
) -> None:
    if principal is None or not principal.active or storage_bytes <= 0:
        return
    try:
        service = quota_service_from_settings(get_settings().gateway)
        factory = get_session_factory()
        async with factory.begin() as session:
            await service.record_usage(
                session,
                principal,
                space_id=space_id or "global",
                storage_bytes=storage_bytes,
            )
    except Exception as exc:
        logger.debug("Quota usage recording skipped", extra={"exception_class": type(exc).__name__})
