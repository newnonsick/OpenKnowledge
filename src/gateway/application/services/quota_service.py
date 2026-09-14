from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
import time

from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.domain.exceptions import QuotaExceededException
from src.gateway.domain.identity import Principal, PrincipalKind
from src.gateway.infrastructure.persistence.identity_models import (
    APIKeyBudgetUsageModel,
    APIKeyQuotaPolicyModel,
)


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    requests_per_minute: int = 120
    concurrent_requests: int = 8
    tokens_per_minute: int = 60000
    storage_bytes: int = 1073741824
    burst_requests: int = 20
    window: timedelta = timedelta(minutes=1)


@dataclass(slots=True)
class _BucketState:
    tokens: float = 0.0
    updated_at: float = 0.0
    in_flight: int = 0


@dataclass(frozen=True, slots=True)
class QuotaUsage:
    credential_id: str | None
    space_id: str | None
    window_started_at: datetime | None
    request_count: int
    token_count: int
    storage_bytes: int
    requests_limit: int
    tokens_limit: int
    storage_limit: int
    concurrent_limit: int
    concurrent_in_flight: int


def quota_scope(principal: Principal, space_id: str | None) -> tuple[str, str]:
    if principal.kind in (PrincipalKind.API_KEY, PrincipalKind.SERVICE) and principal.credential_id:
        credential = f"api_key:{principal.credential_id}"
    else:
        credential = f"member:{principal.subject_id}"
    return credential, space_id or "*"


def _key_uuid(principal: Principal):
    from uuid import UUID

    if principal.kind not in (PrincipalKind.API_KEY, PrincipalKind.SERVICE):
        return None
    if principal.credential_id is None:
        return None
    try:
        return UUID(principal.credential_id)
    except ValueError:
        return None


class QuotaService:
    def __init__(self, policy: QuotaPolicy | None = None) -> None:
        self._policy = policy or QuotaPolicy()
        self._buckets: dict[tuple[str, str], _BucketState] = defaultdict(_BucketState)
        self._lock = asyncio.Lock()

    @property
    def policy(self) -> QuotaPolicy:
        return self._policy

    async def acquire(
        self,
        principal: Principal,
        *,
        space_id: str | None = None,
        tokens: int = 0,
        storage_bytes: int = 0,
        now: float | None = None,
    ) -> None:
        current = now if now is not None else time.monotonic()
        credential, scope_space = quota_scope(principal, space_id)
        rate = self._policy.requests_per_minute / self._policy.window.total_seconds()
        capacity = float(self._policy.requests_per_minute + self._policy.burst_requests)
        async with self._lock:
            bucket = self._buckets[(credential, scope_space)]
            elapsed = max(0.0, current - bucket.updated_at) if bucket.updated_at else capacity / rate
            bucket.tokens = min(capacity, bucket.tokens + elapsed * rate)
            bucket.updated_at = current
            if bucket.tokens < 1.0:
                deficit = 1.0 - bucket.tokens
                retry_after = max(1, math.ceil(deficit / rate)) if rate > 0 else 60
                raise QuotaExceededException(retry_after, quota="requests", limit=self._policy.requests_per_minute)
            if bucket.in_flight >= self._policy.concurrent_requests:
                raise QuotaExceededException(1, quota="concurrency", limit=self._policy.concurrent_requests)
            bucket.tokens -= 1.0
            bucket.in_flight += 1
        if tokens > self._policy.tokens_per_minute:
            async with self._lock:
                bucket.in_flight -= 1
            raise QuotaExceededException(60, quota="tokens", limit=self._policy.tokens_per_minute)
        if storage_bytes > self._policy.storage_bytes:
            async with self._lock:
                bucket.in_flight -= 1
            raise QuotaExceededException(60, quota="storage", limit=self._policy.storage_bytes)

    async def release(
        self,
        principal: Principal,
        *,
        space_id: str | None = None,
    ) -> None:
        credential, scope_space = quota_scope(principal, space_id)
        async with self._lock:
            bucket = self._buckets.get((credential, scope_space))
            if bucket is not None and bucket.in_flight > 0:
                bucket.in_flight -= 1

    async def in_flight(self, principal: Principal, *, space_id: str | None = None) -> int:
        credential, scope_space = quota_scope(principal, space_id)
        async with self._lock:
            bucket = self._buckets.get((credential, scope_space))
            return bucket.in_flight if bucket is not None else 0

    async def record_usage(
        self,
        session: AsyncSession,
        principal: Principal,
        *,
        space_id: str,
        tokens: int = 0,
        storage_bytes: int = 0,
        now: datetime | None = None,
    ) -> None:
        key_id = _key_uuid(principal)
        if key_id is None:
            return
        current_time = now or datetime.now(timezone.utc)
        window_start = current_time.replace(second=0, microsecond=0)
        try:
            row = await session.scalar(
                select(APIKeyBudgetUsageModel).where(
                    APIKeyBudgetUsageModel.api_key_id == key_id,
                    APIKeyBudgetUsageModel.space_id == space_id,
                    APIKeyBudgetUsageModel.window_started_at == window_start,
                )
            )
            if row is None:
                session.add(
                    APIKeyBudgetUsageModel(
                        api_key_id=key_id,
                        space_id=space_id,
                        window_started_at=window_start,
                        request_count=1,
                        token_count=max(0, tokens),
                        storage_bytes=max(0, storage_bytes),
                        updated_at=current_time,
                    )
                )
            else:
                row.request_count += 1
                row.token_count += max(0, tokens)
                row.storage_bytes += max(0, storage_bytes)
                row.updated_at = current_time
            await session.flush()
        except (OperationalError, ProgrammingError):
            await session.rollback()

    async def usage(
        self,
        session: AsyncSession,
        principal: Principal,
        *,
        space_id: str,
        now: datetime | None = None,
    ) -> QuotaUsage | None:
        key_id = _key_uuid(principal)
        if key_id is None:
            return None
        current_time = now or datetime.now(timezone.utc)
        window_start = current_time.replace(second=0, microsecond=0)
        try:
            row = await session.scalar(
                select(APIKeyBudgetUsageModel).where(
                    APIKeyBudgetUsageModel.api_key_id == key_id,
                    APIKeyBudgetUsageModel.space_id == space_id,
                    APIKeyBudgetUsageModel.window_started_at == window_start,
                )
            )
        except (OperationalError, ProgrammingError):
            return None
        policy = await self.effective_policy(session, principal, space_id=space_id)
        inflight = await self.in_flight(principal, space_id=space_id)
        return QuotaUsage(
            credential_id=principal.credential_id,
            space_id=space_id,
            window_started_at=row.window_started_at if row is not None else None,
            request_count=row.request_count if row is not None else 0,
            token_count=row.token_count if row is not None else 0,
            storage_bytes=row.storage_bytes if row is not None else 0,
            requests_limit=policy.requests_per_minute,
            tokens_limit=policy.tokens_per_minute,
            storage_limit=policy.storage_bytes,
            concurrent_limit=policy.concurrent_requests,
            concurrent_in_flight=inflight,
        )

    async def prune(self, session: AsyncSession, *, now: datetime | None = None) -> int:
        current_time = now or datetime.now(timezone.utc)
        cutoff = current_time - self._policy.window - timedelta(minutes=1)
        try:
            result = await session.execute(
                delete(APIKeyBudgetUsageModel).where(APIKeyBudgetUsageModel.window_started_at < cutoff)
            )
            return int(result.rowcount or 0)
        except (OperationalError, ProgrammingError):
            await session.rollback()
            return 0

    async def effective_policy(
        self,
        session: AsyncSession,
        principal: Principal,
        *,
        space_id: str | None = None,
    ) -> QuotaPolicy:
        key_id = _key_uuid(principal)
        if key_id is None:
            return self._policy
        scope_space = space_id or "*"
        try:
            row = await session.scalar(
                select(APIKeyQuotaPolicyModel).where(
                    APIKeyQuotaPolicyModel.api_key_id == key_id,
                    APIKeyQuotaPolicyModel.space_id == scope_space,
                )
            )
            if row is None and scope_space != "*":
                row = await session.scalar(
                    select(APIKeyQuotaPolicyModel).where(
                        APIKeyQuotaPolicyModel.api_key_id == key_id,
                        APIKeyQuotaPolicyModel.space_id == "*",
                    )
                )
        except (OperationalError, ProgrammingError):
            return self._policy
        if row is None:
            return self._policy
        window = (
            timedelta(seconds=row.window_seconds)
            if row.window_seconds is not None
            else self._policy.window
        )
        return QuotaPolicy(
            requests_per_minute=row.requests_per_minute or self._policy.requests_per_minute,
            concurrent_requests=row.concurrent_requests or self._policy.concurrent_requests,
            tokens_per_minute=row.tokens_per_minute or self._policy.tokens_per_minute,
            storage_bytes=row.storage_bytes or self._policy.storage_bytes,
            burst_requests=row.burst_requests if row.burst_requests is not None else self._policy.burst_requests,
            window=window,
        )

    async def set_policy_override(
        self,
        session: AsyncSession,
        principal: Principal,
        *,
        space_id: str | None = None,
        requests_per_minute: int | None = None,
        tokens_per_minute: int | None = None,
        storage_bytes: int | None = None,
        concurrent_requests: int | None = None,
        burst_requests: int | None = None,
        window_seconds: int | None = None,
    ) -> APIKeyQuotaPolicyModel:
        key_id = _key_uuid(principal)
        if key_id is None:
            raise ValueError("Quota overrides require an API key or service credential")
        for label, value in (
            ("requests_per_minute", requests_per_minute),
            ("tokens_per_minute", tokens_per_minute),
            ("storage_bytes", storage_bytes),
            ("concurrent_requests", concurrent_requests),
            ("window_seconds", window_seconds),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"Quota override {label} must be positive")
        if burst_requests is not None and burst_requests < 0:
            raise ValueError("Quota override burst_requests must not be negative")
        scope_space = space_id or "*"
        row = await session.scalar(
            select(APIKeyQuotaPolicyModel).where(
                APIKeyQuotaPolicyModel.api_key_id == key_id,
                APIKeyQuotaPolicyModel.space_id == scope_space,
            )
        )
        if row is None:
            row = APIKeyQuotaPolicyModel(api_key_id=key_id, space_id=scope_space)
            session.add(row)
        row.requests_per_minute = requests_per_minute
        row.tokens_per_minute = tokens_per_minute
        row.storage_bytes = storage_bytes
        row.concurrent_requests = concurrent_requests
        row.burst_requests = burst_requests
        row.window_seconds = window_seconds
        row.updated_at = datetime.now(timezone.utc)
        await session.flush()
        return row

    async def clear_policy_override(
        self,
        session: AsyncSession,
        principal: Principal,
        *,
        space_id: str | None = None,
    ) -> bool:
        key_id = _key_uuid(principal)
        if key_id is None:
            return False
        row = await session.scalar(
            select(APIKeyQuotaPolicyModel).where(
                APIKeyQuotaPolicyModel.api_key_id == key_id,
                APIKeyQuotaPolicyModel.space_id == (space_id or "*"),
            )
        )
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        return True

    async def check_persistent_window(
        self,
        session: AsyncSession,
        principal: Principal,
        *,
        space_id: str,
        now: datetime | None = None,
    ) -> None:
        key_id = _key_uuid(principal)
        if key_id is None:
            return
        policy = await self.effective_policy(session, principal, space_id=space_id)
        current_time = now or datetime.now(timezone.utc)
        window_start = current_time.replace(second=0, microsecond=0)
        try:
            row = await session.scalar(
                select(APIKeyBudgetUsageModel).where(
                    APIKeyBudgetUsageModel.api_key_id == key_id,
                    APIKeyBudgetUsageModel.space_id == space_id,
                    APIKeyBudgetUsageModel.window_started_at == window_start,
                )
            )
        except (OperationalError, ProgrammingError):
            return
        if row is not None and row.request_count >= policy.requests_per_minute:
            raise QuotaExceededException(60, quota="requests", limit=policy.requests_per_minute)
        if row is not None and row.token_count >= policy.tokens_per_minute:
            raise QuotaExceededException(60, quota="tokens", limit=policy.tokens_per_minute)
        if row is not None and row.storage_bytes >= policy.storage_bytes:
            raise QuotaExceededException(60, quota="storage", limit=policy.storage_bytes)


@dataclass(slots=True)
class QuotaGuard:
    service: QuotaService
    principal: Principal
    space_id: str | None = None

    async def __aenter__(self) -> QuotaGuard:
        await self.service.acquire(self.principal, space_id=self.space_id)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.service.release(self.principal, space_id=self.space_id)


_QUOTA_SERVICES: dict[tuple[int, int, int, int, int], QuotaService] = {}
_QUOTA_FIELD_DEFAULTS: dict[str, int] = {}


def quota_service_from_settings(gateway_settings) -> QuotaService:
    key = (
        int(getattr(gateway_settings, "quota_requests_per_minute", 120)),
        int(getattr(gateway_settings, "quota_concurrent_requests", 8)),
        int(getattr(gateway_settings, "quota_tokens_per_minute", 60000)),
        int(getattr(gateway_settings, "quota_storage_bytes", 1073741824)),
        int(getattr(gateway_settings, "quota_burst_requests", 20)),
    )
    service = _QUOTA_SERVICES.get(key)
    if service is None:
        service = QuotaService(
            QuotaPolicy(
                requests_per_minute=key[0],
                concurrent_requests=key[1],
                tokens_per_minute=key[2],
                storage_bytes=key[3],
                burst_requests=key[4],
            )
        )
        _QUOTA_SERVICES[key] = service
    return service
