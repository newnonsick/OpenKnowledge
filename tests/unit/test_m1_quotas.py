from datetime import timedelta
from uuid import uuid4

import pytest

from src.gateway.application.services.quota_service import (
    QuotaPolicy,
    QuotaService,
    quota_scope,
)
from src.gateway.domain.exceptions import QuotaExceededException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.presentation.quotas import extract_quota_space


def api_principal(credential_id=None) -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read"}),
        credential_id=credential_id or str(uuid4()),
    )


def session_principal() -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"*"}),
        credential_id=str(uuid4()),
    )


def test_quota_scope_isolates_credentials_and_spaces() -> None:
    first = api_principal("credential-one")
    second = api_principal("credential-two")
    assert quota_scope(first, "space-a") == ("api_key:credential-one", "space-a")
    assert quota_scope(first, "space-a") != quota_scope(second, "space-a")
    assert quota_scope(first, "space-a") != quota_scope(first, "space-b")
    assert quota_scope(session_principal(), None)[1] == "*"


async def test_rate_limit_rejects_with_retry_after_and_recovers() -> None:
    service = QuotaService(QuotaPolicy(requests_per_minute=2, burst_requests=0, concurrent_requests=8))
    principal = api_principal()
    await service.acquire(principal, space_id="space-a", now=1000.0)
    await service.acquire(principal, space_id="space-a", now=1000.0)
    await service.release(principal, space_id="space-a")
    await service.release(principal, space_id="space-a")
    with pytest.raises(QuotaExceededException) as exc_info:
        await service.acquire(principal, space_id="space-a", now=1000.0)
    assert exc_info.value.status_code == 429
    assert exc_info.value.details["retry_after_seconds"] >= 1
    assert exc_info.value.code == "quota_exceeded"
    await service.acquire(principal, space_id="space-a", now=1031.0)
    await service.release(principal, space_id="space-a")


async def test_concurrency_limit_prevents_starvation_across_credentials() -> None:
    service = QuotaService(QuotaPolicy(requests_per_minute=1000, burst_requests=1000, concurrent_requests=1))
    greedy = api_principal()
    other = api_principal()
    await service.acquire(greedy, space_id="space-a", now=2000.0)
    with pytest.raises(QuotaExceededException) as exc_info:
        await service.acquire(greedy, space_id="space-a", now=2000.0)
    assert exc_info.value.quota == "concurrency"
    await service.acquire(other, space_id="space-a", now=2000.0)
    await service.release(greedy, space_id="space-a")
    await service.release(other, space_id="space-a")
    assert await service.in_flight(greedy, space_id="space-a") == 0


async def test_quota_buckets_are_per_space() -> None:
    service = QuotaService(QuotaPolicy(requests_per_minute=1, burst_requests=0, concurrent_requests=8))
    principal = api_principal()
    await service.acquire(principal, space_id="space-a", now=3000.0)
    await service.release(principal, space_id="space-a")
    await service.acquire(principal, space_id="space-b", now=3000.0)
    await service.release(principal, space_id="space-b")
    with pytest.raises(QuotaExceededException):
        await service.acquire(principal, space_id="space-a", now=3000.0)
    await service.release(principal, space_id="space-a")


async def test_oversized_token_and_storage_claims_are_rejected() -> None:
    service = QuotaService(QuotaPolicy(tokens_per_minute=100, storage_bytes=1024))
    principal = api_principal()
    with pytest.raises(QuotaExceededException) as token_exc:
        await service.acquire(principal, space_id="space-a", tokens=101, now=4000.0)
    assert token_exc.value.quota == "tokens"
    assert await service.in_flight(principal, space_id="space-a") == 0
    with pytest.raises(QuotaExceededException) as storage_exc:
        await service.acquire(principal, space_id="space-a", storage_bytes=2048, now=4000.0)
    assert storage_exc.value.quota == "storage"


def test_space_extraction_prefers_query_then_body() -> None:
    assert extract_quota_space({"space_id": "query-space"}, {"space_id": "body-space"}) == "query-space"
    assert extract_quota_space({}, {"space_id": "body-space"}) == "body-space"
    assert extract_quota_space({}, {"space_ids": ["first", "second"]}) == "first"
    assert extract_quota_space({}, {"workspace_id": "chat-space"}) == "chat-space"
    assert extract_quota_space({"active_space_id": "active"}, {}) == "active"
    assert extract_quota_space({}, {}) is None
    assert extract_quota_space({"space_id": "  "}, {}) is None


def test_quota_policy_window_defaults_to_one_minute() -> None:
    assert QuotaPolicy().window == timedelta(minutes=1)
