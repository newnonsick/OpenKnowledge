from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import JSON, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.gateway.application.services.webhook_delivery_service import (
    DELIVERY_BACKOFF_BASE_SECONDS,
    DELIVERY_BACKOFF_CAP_SECONDS,
    WebhookDeliveryService,
    WebhookSubscriptionService,
    delivery_backoff,
)
from src.gateway.application.services.webhook_service import (
    serialize_canonical,
    verify_webhook_signature,
)
from src.gateway.domain.exceptions import AuthorizationException, ValidationException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import (
    WebhookDeliveryModel,
    WebhookSubscriptionModel,
)
from src.gateway.infrastructure.persistence.models import Base, Workspace


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):
    return compiler.visit_JSON(JSON(), **kw)


@pytest.fixture()
async def factory():
    from sqlalchemy import MetaData

    moved = MetaData()
    tables = []
    for source in (
        Workspace.__table__,
        MemberModel.__table__,
        SpaceMembershipModel.__table__,
        WebhookSubscriptionModel.__table__,
        WebhookDeliveryModel.__table__,
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


def _principal(member_id) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read", "knowledge:write", "spaces:read", "spaces:write"}),
    )


async def _seed_member(factory, *, space_id="space-a"):
    from src.gateway.domain.identity import normalize_username

    session = factory()
    member_id = uuid4()
    now = datetime.now(timezone.utc)
    session.add(Workspace(id=space_id, name="Space A", created_at=now))
    session.add(
        MemberModel(
            id=member_id,
            username="ada@example.test",
            username_normalized=normalize_username("ada@example.test"),
            display_name="Ada",
            status="active",
            system_role="member",
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        SpaceMembershipModel(
            space_id=space_id, member_id=member_id, role="owner",
            created_at=now, updated_at=now,
        )
    )
    await session.commit()
    return session, member_id


def test_delivery_backoff_grows_then_caps() -> None:
    assert delivery_backoff(1) == timedelta(seconds=DELIVERY_BACKOFF_BASE_SECONDS)
    assert delivery_backoff(2) == timedelta(seconds=DELIVERY_BACKOFF_BASE_SECONDS * 2)
    assert delivery_backoff(100) == timedelta(seconds=DELIVERY_BACKOFF_CAP_SECONDS)


async def test_register_rejects_loopback_and_plain_http(factory) -> None:
    session, member_id = await _seed_member(factory)
    service = WebhookSubscriptionService(session)
    with pytest.raises(ValidationException):
        await service.register(
            _principal(member_id), space_id="space-a", url="http://localhost:9/hook",
            secret="s", request_id="r1",
        )
    with pytest.raises(ValidationException):
        await service.register(
            _principal(member_id), space_id="space-a", url="http://example.com/hook",
            secret="s", request_id="r1",
        )
    await session.close()


async def test_register_outside_space_denied(factory) -> None:
    session, member_id = await _seed_member(factory)
    service = WebhookSubscriptionService(session)
    with pytest.raises(AuthorizationException):
        await service.register(
            _principal(member_id), space_id="space-b", url="https://example.com/hook",
            secret="s", request_id="r1",
        )
    await session.close()


async def test_enqueue_is_idempotent_per_deduplication_key(factory) -> None:
    session, member_id = await _seed_member(factory)
    subscription = await WebhookSubscriptionService(session).register(
        _principal(member_id), space_id="space-a", url="https://example.com/hook",
        secret="s", request_id="r1",
    )
    service = WebhookDeliveryService(session)
    first = await service.enqueue(
        subscription_id=subscription.id, event_type="ingestion.succeeded",
        deduplication_key="evt-1", payload={"ok": True},
    )
    second = await service.enqueue(
        subscription_id=subscription.id, event_type="ingestion.succeeded",
        deduplication_key="evt-1", payload={"ok": True},
    )
    assert first.id == second.id
    await session.close()


async def test_deliver_signs_payload_and_marks_delivered(factory) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["signature"] = request.headers["x-openknowledge-signature"]
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    session, member_id = await _seed_member(factory)
    subscription = await WebhookSubscriptionService(session).register(
        _principal(member_id), space_id="space-a", url="https://example.com/hook",
        secret="topsecret", request_id="r1",
    )
    service = WebhookDeliveryService(session, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await service.enqueue(
        subscription_id=subscription.id, event_type="ingestion.succeeded",
        deduplication_key="evt-9", payload={"ok": True},
    )
    claim = await service.claim_next("worker-1")
    assert claim is not None
    assert await service.deliver(claim) is True
    signature = seen["signature"]
    timestamp = signature.split("t=")[1].split(",")[0]
    digest = signature.split("v1=")[1]
    assert verify_webhook_signature(digest, seen["body"], secret="topsecret", timestamp=timestamp) is True
    stored = await session.get(WebhookDeliveryModel, claim.delivery_id)
    assert stored.state == "delivered"
    await session.close()


async def test_deliver_retries_then_succeeds(factory) -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(500, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    session, member_id = await _seed_member(factory)
    subscription = await WebhookSubscriptionService(session).register(
        _principal(member_id), space_id="space-a", url="https://example.com/hook",
        secret="s", request_id="r1",
    )
    service = WebhookDeliveryService(session, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await service.enqueue(
        subscription_id=subscription.id, event_type="ingestion.failed",
        deduplication_key="evt-r", payload={},
    )
    for _ in range(3):
        claim = await service.claim_next("worker-1")
        assert claim is not None
        done = await service.deliver(claim)
        if done:
            break
        db_now = await session.scalar(select(func.now()))
        stored = await session.get(WebhookDeliveryModel, claim.delivery_id)
        stored.available_at = db_now - timedelta(seconds=1)
        await session.flush()
    rows = list(await session.scalars(select(WebhookDeliveryModel)))
    assert rows[0].state == "delivered"
    assert rows[0].attempt_count == 3
    await session.close()


async def test_revoked_subscriber_delivery_fails_closed(factory) -> None:
    session, member_id = await _seed_member(factory)
    subscriptions = WebhookSubscriptionService(session)
    subscription = await subscriptions.register(
        _principal(member_id), space_id="space-a", url="https://example.com/hook",
        secret="s", request_id="r1",
    )
    deliveries = WebhookDeliveryService(session)
    await deliveries.enqueue(
        subscription_id=subscription.id, event_type="ingestion.succeeded",
        deduplication_key="evt-x", payload={},
    )
    await subscriptions.revoke(_principal(member_id), subscription_id=subscription.id)
    assert await deliveries.claim_next("worker-1") is None
    await session.close()


def test_webhook_dns_names_resolving_internal_are_rejected():
    import socket

    from src.gateway.application.services.webhook_delivery_service import _resolve_webhook_host
    from src.gateway.domain.exceptions import ValidationException

    real_getaddrinfo = socket.getaddrinfo

    class _FakeLoop:
        async def getaddrinfo(self, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    import asyncio

    async def run() -> None:
        loop = _FakeLoop()
        real = asyncio.get_running_loop
        asyncio.get_running_loop = lambda: loop  # type: ignore[assignment]
        try:
            await _resolve_webhook_host("internal.example")
        finally:
            asyncio.get_running_loop = real  # type: ignore[assignment]

    with __import__("pytest").raises(ValidationException):
        asyncio.run(run())
    assert real_getaddrinfo is socket.getaddrinfo


async def test_register_encrypts_secret_when_keys_configured(factory, monkeypatch) -> None:
    from cryptography.fernet import Fernet

    import src.gateway.application.services.webhook_delivery_service as delivery_module
    from src.gateway.application.security.tokens import SecretValue
    from src.gateway.application.security.webhook_secrets import WebhookSecretService

    service = WebhookSecretService(SecretValue(Fernet.generate_key().decode("ascii")))
    monkeypatch.setattr(delivery_module, "_webhook_secret_service", lambda: service)
    session, member_id = await _seed_member(factory)
    subscription = await WebhookSubscriptionService(session).register(
        _principal(member_id), space_id="space-a", url="https://example.com/hook",
        secret="hooksecret", request_id="r-enc",
    )
    assert subscription.secret == ""
    assert subscription.secret_ciphertext
    assert subscription.secret_key_version == 1
    assert service.decrypt_secret(bytes(subscription.secret_ciphertext)).reveal() == "hooksecret"
    await session.close()


async def test_deliver_migrates_legacy_plaintext_secret(factory, monkeypatch) -> None:
    from cryptography.fernet import Fernet

    import src.gateway.application.services.webhook_delivery_service as delivery_module
    from src.gateway.application.security.tokens import SecretValue
    from src.gateway.application.security.webhook_secrets import WebhookSecretService

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["signature"] = request.headers["x-openknowledge-signature"]
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    service = WebhookSecretService(SecretValue(Fernet.generate_key().decode("ascii")))
    monkeypatch.setattr(delivery_module, "_webhook_secret_service", lambda: service)
    async def _public(hostname): return ["93.184.216.34"]
    monkeypatch.setattr(delivery_module, "_resolve_host_addresses", _public)
    session, member_id = await _seed_member(factory)
    subscription = await WebhookSubscriptionService(session).register(
        _principal(member_id), space_id="space-a", url="https://example.com/hook",
        secret="legacysecret", request_id="r-leg",
    )
    # simulate a pre-encryption row
    subscription.secret_ciphertext = None
    subscription.secret_key_version = None
    subscription.secret = "legacysecret"
    delivery_service = WebhookDeliveryService(session, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await delivery_service.enqueue(
        subscription_id=subscription.id, event_type="ingestion.succeeded",
        deduplication_key="evt-leg", payload={"ok": True},
    )
    claim = await delivery_service.claim_next("worker-1")
    assert claim is not None
    assert await delivery_service.deliver(claim) is True
    signature = seen["signature"]
    timestamp = signature.split("t=")[1].split(",")[0]
    digest = signature.split("v1=")[1]
    assert verify_webhook_signature(digest, seen["body"], secret="legacysecret", timestamp=timestamp) is True
    refreshed = await session.get(WebhookSubscriptionModel, subscription.id)
    assert refreshed.secret == ""
    assert refreshed.secret_ciphertext
    await session.close()


async def test_deliver_rejects_rebound_internal_target(factory, monkeypatch) -> None:
    import src.gateway.application.services.webhook_delivery_service as delivery_module

    session, member_id = await _seed_member(factory)
    subscription = await WebhookSubscriptionService(session).register(
        _principal(member_id), space_id="space-a", url="https://example.com/hook",
        secret="s", request_id="r-rebind",
    )
    async def _loopback(hostname): return ["127.0.0.1"]
    monkeypatch.setattr(delivery_module, "_resolve_host_addresses", _loopback)
    service = WebhookDeliveryService(session, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))))
    await service.enqueue(
        subscription_id=subscription.id, event_type="ingestion.succeeded",
        deduplication_key="evt-rebind", payload={"ok": True},
    )
    claim = await service.claim_next("worker-1")
    assert claim is not None
    assert await service.deliver(claim) is False
    stored = await session.get(WebhookDeliveryModel, claim.delivery_id)
    assert stored.state == "failed"
    assert stored.last_error_code == "internal_target"
    await session.close()


async def test_deliver_marks_decrypt_failure_terminal(factory, monkeypatch) -> None:
    from cryptography.fernet import Fernet

    import src.gateway.application.services.webhook_delivery_service as delivery_module
    from src.gateway.application.security.tokens import SecretValue
    from src.gateway.application.security.webhook_secrets import WebhookSecretService

    good = WebhookSecretService(SecretValue(Fernet.generate_key().decode("ascii")))
    bad = WebhookSecretService(SecretValue(Fernet.generate_key().decode("ascii")))
    monkeypatch.setattr(delivery_module, "_webhook_secret_service", lambda: good)
    session, member_id = await _seed_member(factory)
    subscription = await WebhookSubscriptionService(session).register(
        _principal(member_id), space_id="space-a", url="https://example.com/hook",
        secret="s", request_id="r-encfail",
    )
    subscription.secret_ciphertext = bad.encrypt_secret(SecretValue("s"))
    subscription.secret_key_version = 1
    subscription.secret = ""
    service = WebhookDeliveryService(session, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))))
    await service.enqueue(
        subscription_id=subscription.id, event_type="ingestion.succeeded",
        deduplication_key="evt-encfail", payload={"ok": True},
    )
    claim = await service.claim_next("worker-1")
    assert claim is not None
    assert await service.deliver(claim) is False
    stored = await session.get(WebhookDeliveryModel, claim.delivery_id)
    assert stored.state == "failed"
    assert stored.last_error_code == "decrypt_failed"
    await session.close()
