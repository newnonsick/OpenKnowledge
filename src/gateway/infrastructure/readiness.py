from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.gateway.infrastructure.database import normalize_database_url


logger = logging.getLogger(__name__)


class ReadinessProbe:
    def __init__(
        self,
        database_url: str,
        ttl_seconds: float = 1.0,
        timeout_seconds: float = 2.0,
        engine_factory: Callable[..., Any] = create_async_engine,
    ) -> None:
        self.database_url = normalize_database_url(database_url)
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self.engine_factory = engine_factory
        self._lock = asyncio.Lock()
        self._last_checked = float("-inf")
        self._cached_result: bool | None = None

    async def check(self) -> bool:
        now = time.monotonic()
        if self._cached_result is not None and now - self._last_checked < self.ttl_seconds:
            return self._cached_result

        async with self._lock:
            now = time.monotonic()
            if self._cached_result is not None and now - self._last_checked < self.ttl_seconds:
                return self._cached_result

            try:
                result = await asyncio.wait_for(
                    self._probe_database(),
                    timeout=self.timeout_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "Readiness dependency failed",
                    extra={"exception_class": type(exc).__name__},
                )
                result = False

            self._cached_result = result
            self._last_checked = time.monotonic()
            return result

    async def _probe_database(self) -> bool:
        engine = self.engine_factory(self.database_url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        finally:
            await engine.dispose()
