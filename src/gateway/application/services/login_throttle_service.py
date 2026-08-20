from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress

from fastapi import Request
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.domain.exceptions import RateLimitException
from src.gateway.domain.identity import normalize_username
from src.gateway.infrastructure.persistence.identity_models import LoginThrottleBucketModel


@dataclass(frozen=True, slots=True)
class ThrottleRule:
    limit: int
    window: timedelta
    base_block: timedelta
    max_block: timedelta

    @property
    def window_seconds(self) -> int:
        return int(self.window.total_seconds())

    def block_seconds(self, failure_count: int) -> int:
        if failure_count < self.limit:
            return 0
        exponent = min(failure_count - self.limit, 30)
        return min(
            int(self.max_block.total_seconds()),
            int(self.base_block.total_seconds()) * (2**exponent),
        )


@dataclass(frozen=True, slots=True)
class LoginThrottlePolicy:
    account: ThrottleRule = ThrottleRule(5, timedelta(minutes=15), timedelta(seconds=2), timedelta(minutes=15))
    ip: ThrottleRule = ThrottleRule(20, timedelta(minutes=15), timedelta(seconds=2), timedelta(minutes=15))
    global_: ThrottleRule = ThrottleRule(200, timedelta(minutes=1), timedelta(seconds=1), timedelta(minutes=1))


class LoginThrottleService:
    def __init__(self, session: AsyncSession, policy: LoginThrottlePolicy | None = None) -> None:
        self._session = session
        self._policy = policy or LoginThrottlePolicy()

    async def assert_allowed(self, username: str, client_ip: str, *, now: datetime | None = None) -> None:
        current_time = now or datetime.now(timezone.utc)
        keys = self._keys(username, client_ip)
        buckets = list(
            await self._session.scalars(
                select(LoginThrottleBucketModel).where(
                    LoginThrottleBucketModel.bucket_key.in_([key for _, key, _ in keys])
                )
            )
        )
        retry_after = max(
            (
                max(0, int((bucket.blocked_until - current_time).total_seconds()) + 1)
                for bucket in buckets
                if bucket.blocked_until is not None and bucket.blocked_until > current_time
            ),
            default=0,
        )
        if retry_after:
            raise RateLimitException(retry_after)

    async def record_failure(self, username: str, client_ip: str, *, now: datetime | None = None) -> int:
        current_time = now or datetime.now(timezone.utc)
        retry_after = 0
        for bucket_type, bucket_key, rule in self._keys(username, client_ip):
            blocked_until = await self._session.scalar(
                text(
                    """
                    INSERT INTO login_throttle_buckets (
                        bucket_type, bucket_key, failure_count, window_started_at, blocked_until, updated_at
                    ) VALUES (
                        :bucket_type, :bucket_key, 1, :now, NULL, :now
                    )
                    ON CONFLICT (bucket_type, bucket_key) DO UPDATE SET
                        failure_count = CASE
                            WHEN login_throttle_buckets.window_started_at <= :window_cutoff THEN 1
                            ELSE login_throttle_buckets.failure_count + 1
                        END,
                        window_started_at = CASE
                            WHEN login_throttle_buckets.window_started_at <= :window_cutoff THEN :now
                            ELSE login_throttle_buckets.window_started_at
                        END,
                        blocked_until = CASE
                            WHEN (
                                CASE
                                    WHEN login_throttle_buckets.window_started_at <= :window_cutoff THEN 1
                                    ELSE login_throttle_buckets.failure_count + 1
                                END
                            ) >= :failure_limit
                            THEN :now + make_interval(secs => LEAST(
                                :max_block_seconds,
                                :base_block_seconds * power(
                                    2,
                                    LEAST(
                                        (
                                            CASE
                                                WHEN login_throttle_buckets.window_started_at <= :window_cutoff THEN 1
                                                ELSE login_throttle_buckets.failure_count + 1
                                            END
                                        ) - :failure_limit,
                                        30
                                    )
                                )
                            )::double precision)
                            ELSE NULL
                        END,
                        updated_at = :now
                    RETURNING blocked_until
                    """
                ),
                {
                    "bucket_type": bucket_type,
                    "bucket_key": bucket_key,
                    "now": current_time,
                    "window_cutoff": current_time - rule.window,
                    "failure_limit": rule.limit,
                    "base_block_seconds": int(rule.base_block.total_seconds()),
                    "max_block_seconds": int(rule.max_block.total_seconds()),
                },
            )
            if blocked_until is not None and blocked_until > current_time:
                retry_after = max(retry_after, int((blocked_until - current_time).total_seconds()) + 1)
        return retry_after

    async def record_success(self, username: str) -> None:
        await self._session.execute(
            delete(LoginThrottleBucketModel).where(
                LoginThrottleBucketModel.bucket_type == "account",
                LoginThrottleBucketModel.bucket_key == self._digest(normalize_username(username)),
            )
        )

    def _keys(self, username: str, client_ip: str) -> tuple[tuple[str, str, ThrottleRule], ...]:
        return (
            ("account", self._digest(normalize_username(username)), self._policy.account),
            ("ip", self._digest(client_ip), self._policy.ip),
            ("global", self._digest("global"), self._policy.global_),
        )

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


def request_client_ip(request: Request, trusted_proxy_cidrs: list[str]) -> str:
    direct = request.client.host if request.client is not None else "0.0.0.0"
    try:
        direct_ip = ipaddress.ip_address(direct)
    except ValueError:
        return "0.0.0.0"
    trusted = any(direct_ip in ipaddress.ip_network(value, strict=False) for value in trusted_proxy_cidrs)
    if not trusted:
        return direct_ip.compressed
    forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
    try:
        return ipaddress.ip_address(forwarded).compressed
    except ValueError:
        return direct_ip.compressed
