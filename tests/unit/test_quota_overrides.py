from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import JSON, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.gateway.application.services.quota_service import QuotaPolicy, QuotaService
from src.gateway.domain.exceptions import QuotaExceededException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.identity_models import (
    APIKeyBudgetUsageModel,
    APIKeyQuotaPolicyModel,
    MemberModel,
    PersonalAPIKeyModel,
)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):
    return compiler.visit_JSON(JSON(), **kw)


@pytest.fixture()
async def factory():
    from sqlalchemy import MetaData

    moved = MetaData()
    tables = []
    for source in (
        MemberModel.__table__,
        PersonalAPIKeyModel.__table__,
        APIKeyBudgetUsageModel.__table__,
        APIKeyQuotaPolicyModel.__table__,
    ):
        copied = source.to_metadata(moved)
        for column in copied.columns:
            if column.server_default is not None and "::" in str(column.server_default.arg):
                column.server_default = None
        copied.constraints = {
            constraint
            for constraint in copied.constraints
            if "char_length" not in str(getattr(constraint, "sqltext", ""))
        }
        tables.append(copied)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(moved.create_all, tables=tables)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


def _principal(credential: str) -> Principal:
    return Principal(
        subject_id=str(uuid4()),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read"}),
        credential_id=credential,
    )


async def test_over_quota_credential_rejected_while_other_succeeds(factory) -> None:
    service = QuotaService(QuotaPolicy(requests_per_minute=2, burst_requests=0))
    session = factory()
    now = datetime.now(timezone.utc)
    heavy = _principal(str(uuid4()))
    light = _principal(str(uuid4()))
    for _ in range(2):
        await service.record_usage(session, heavy, space_id="space-a", now=now)
    with pytest.raises(QuotaExceededException) as excinfo:
        await service.check_persistent_window(session, heavy, space_id="space-a", now=now)
    assert excinfo.value.details.get("retry_after_seconds", 1) >= 1
    await service.check_persistent_window(session, light, space_id="space-a", now=now)
    await session.close()


async def test_policy_override_resolves_per_space_with_global_fallback(factory) -> None:
    service = QuotaService(QuotaPolicy(requests_per_minute=120))
    session = factory()
    key = _principal(str(uuid4()))
    await service.set_policy_override(session, key, space_id="space-a", requests_per_minute=3)
    scoped = await service.effective_policy(session, key, space_id="space-a")
    assert scoped.requests_per_minute == 3
    fallback = await service.effective_policy(session, key, space_id="space-b")
    assert fallback.requests_per_minute == 120
    await service.set_policy_override(session, key, requests_per_minute=7)
    assert (await service.effective_policy(session, key, space_id="space-b")).requests_per_minute == 7
    assert (await service.effective_policy(session, key, space_id="space-a")).requests_per_minute == 3
    assert await service.clear_policy_override(session, key, space_id="space-a") is True
    assert (await service.effective_policy(session, key, space_id="space-a")).requests_per_minute == 7
    assert await service.clear_policy_override(session, key, space_id="space-a") is False
    with pytest.raises(ValueError):
        await service.set_policy_override(session, key, requests_per_minute=0)
    await session.close()


async def test_override_enforced_by_persistent_window(factory) -> None:
    service = QuotaService(QuotaPolicy(requests_per_minute=120))
    session = factory()
    now = datetime.now(timezone.utc)
    key = _principal(str(uuid4()))
    await service.set_policy_override(session, key, space_id="space-a", requests_per_minute=1)
    await service.record_usage(session, key, space_id="space-a", now=now)
    with pytest.raises(QuotaExceededException):
        await service.check_persistent_window(session, key, space_id="space-a", now=now)
    await session.close()
