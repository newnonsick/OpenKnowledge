from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.tokens import SecretValue
from src.gateway.application.security.webhook_secrets import WebhookSecretService
from src.gateway.application.services.webhook_service import (
    serialize_canonical,
    sign_webhook_payload,
    webhook_timestamp,
)
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import AuthorizationException, ValidationException
from src.gateway.domain.authorization import Action
from src.gateway.domain.identity import Principal, SpaceRole
from src.gateway.infrastructure.persistence.ingestion_models import (
    WebhookDeliveryModel,
    WebhookSubscriptionModel,
)


logger = logging.getLogger(__name__)

DELIVERY_MAX_ATTEMPTS = 8
DELIVERY_BACKOFF_BASE_SECONDS = 30
DELIVERY_BACKOFF_CAP_SECONDS = 3600
DELIVERY_LEASE_SECONDS = 300
DELIVERY_TIMEOUT_SECONDS = 15


def delivery_backoff(attempt: int) -> timedelta:
    delay = DELIVERY_BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1))
    return timedelta(seconds=min(delay, DELIVERY_BACKOFF_CAP_SECONDS))


def _validate_webhook_url(url: str) -> str:
    normalized_url = url.strip()
    if not normalized_url or len(normalized_url) > 2048:
        raise ValidationException("Webhook URL must contain between 1 and 2048 characters.")
    parsed = urlparse(normalized_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValidationException("Webhook URL must use https.")
    hostname = parsed.hostname.strip("[]")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        lowered = hostname.lower()
        if lowered == "localhost" or lowered.endswith(".localhost"):
            raise ValidationException("Webhook URL must not target loopback addresses.")
    else:
        if (
            address.is_loopback
            or address.is_unspecified
            or address.is_link_local
            or address.is_private
            or address.is_reserved
        ):
            raise ValidationException("Webhook URL must not target internal addresses.")
    return normalized_url


async def _resolve_webhook_host(hostname: str) -> None:
    for address in await _resolve_host_addresses(hostname):
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            raise ValidationException("Webhook URL must not target internal addresses.")
        if (
            parsed.is_loopback
            or parsed.is_unspecified
            or parsed.is_link_local
            or parsed.is_private
            or parsed.is_reserved
            or parsed.is_multicast
        ):
            raise ValidationException("Webhook URL must not target internal addresses.")


async def _resolve_host_addresses(hostname: str) -> list[str]:
    try:
        resolved = await asyncio.get_running_loop().getaddrinfo(
            hostname, 443, type=socket.SOCK_STREAM
        )
    except (OSError, UnicodeError) as exc:
        raise ValidationException("Webhook URL host could not be resolved.") from exc
    addresses = []
    for family, _, _, _, sockaddr in resolved:
        address = sockaddr[0]
        if family == socket.AF_INET6:
            address = address.strip("[]")
        addresses.append(address)
    return addresses


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    delivery_id: UUID
    subscription_id: UUID
    claim_token: UUID


def _webhook_secret_service() -> WebhookSecretService | None:
    gateway = get_settings().gateway
    if not gateway.webhook_encryption_keys:
        return None
    return WebhookSecretService(
        {version: SecretValue(value) for version, value in gateway.webhook_encryption_keys.items()},
        active_key_version=gateway.active_webhook_encryption_key_version,
    )


def _reveal_subscription_secret(subscription: WebhookSubscriptionModel) -> str:
    if subscription.secret_ciphertext:
        service = _webhook_secret_service()
        if service is not None:
            return service.decrypt_secret(
                bytes(subscription.secret_ciphertext),
                key_version=subscription.secret_key_version or service.active_key_version,
            ).reveal()
    return subscription.secret


class WebhookSubscriptionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(
        self,
        principal: Principal,
        *,
        space_id: str,
        url: str,
        secret: str,
        event_filter: tuple[str, ...] = (),
        request_id: str,
        now: datetime | None = None,
    ) -> WebhookSubscriptionModel:
        from src.gateway.application.services.authorization_service import AuthorizationService

        normalized_url = _validate_webhook_url(url)
        await _resolve_webhook_host(urlparse(normalized_url).hostname or "")
        if not secret or len(secret) > 512:
            raise ValidationException("Webhook secret must contain between 1 and 512 characters.")
        try:
            member_id = UUID(principal.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
        role = await AuthorizationService(self._session).authorize_space(
            principal, space_id, Action.SPACE_UPDATE
        )
        if role not in (SpaceRole.OWNER, SpaceRole.EDITOR):
            raise AuthorizationException()
        model = WebhookSubscriptionModel(
            space_id=space_id,
            url=normalized_url,
            secret=secret,
            event_filter=list(event_filter),
            created_by_member_id=member_id,
        )
        service = _webhook_secret_service()
        if service is not None:
            model.secret_ciphertext = service.encrypt_secret(SecretValue(secret))
            model.secret_key_version = service.active_key_version
            model.secret = ""
        self._session.add(model)
        await self._session.flush()
        return model

    async def revoke(
        self,
        principal: Principal,
        *,
        subscription_id: UUID,
    ) -> WebhookSubscriptionModel:
        subscription = await self._session.get(WebhookSubscriptionModel, subscription_id)
        if subscription is None or subscription.status != "active":
            raise AuthorizationException()
        from src.gateway.application.services.authorization_service import AuthorizationService

        role = await AuthorizationService(self._session).authorize_space(
            principal, subscription.space_id, Action.SPACE_UPDATE
        )
        if role not in (SpaceRole.OWNER, SpaceRole.EDITOR):
            raise AuthorizationException()
        subscription.status = "revoked"
        subscription.revoked_at = datetime.now(timezone.utc)
        await self._session.flush()
        return subscription

    async def list_for_space(
        self, principal: Principal, *, space_id: str
    ) -> list[WebhookSubscriptionModel]:
        from src.gateway.application.services.authorization_service import AuthorizationService

        await AuthorizationService(self._session).authorize_space(
            principal, space_id, Action.SPACE_READ
        )
        return list(
            await self._session.scalars(
                select(WebhookSubscriptionModel)
                .where(WebhookSubscriptionModel.space_id == space_id)
                .order_by(WebhookSubscriptionModel.created_at.asc())
            )
        )


class WebhookDeliveryService:
    def __init__(self, session: AsyncSession, *, client: httpx.AsyncClient | None = None) -> None:
        self._session = session
        self._client = client

    async def enqueue(
        self,
        *,
        subscription_id: UUID,
        event_type: str,
        deduplication_key: str,
        payload: dict,
        max_attempts: int = DELIVERY_MAX_ATTEMPTS,
    ) -> WebhookDeliveryModel:
        if not isinstance(event_type, str) or not event_type or len(event_type) > 64:
            raise ValidationException("Webhook event type must contain between 1 and 64 characters.")
        if not isinstance(deduplication_key, str) or not deduplication_key or len(deduplication_key) > 200:
            raise ValidationException("Webhook deduplication key must contain between 1 and 200 characters.")
        if max_attempts < 1:
            raise ValidationException("Webhook max attempts must be positive.")
        existing = await self._session.scalar(
            select(WebhookDeliveryModel).where(
                WebhookDeliveryModel.subscription_id == subscription_id,
                WebhookDeliveryModel.deduplication_key == deduplication_key,
            )
        )
        if existing is not None:
            return existing
        model = WebhookDeliveryModel(
            subscription_id=subscription_id,
            event_type=event_type,
            deduplication_key=deduplication_key,
            payload=dict(payload),
            max_attempts=max_attempts,
        )
        self._session.add(model)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError:
            return await self._session.scalar(
                select(WebhookDeliveryModel).where(
                    WebhookDeliveryModel.subscription_id == subscription_id,
                    WebhookDeliveryModel.deduplication_key == deduplication_key,
                )
            )
        return model

    async def claim_next(self, worker_id: str) -> DeliveryClaim | None:
        if not worker_id.strip():
            raise ValidationException("A delivery worker id is required.")
        now = await self._session.scalar(select(func.now()))
        await self._session.execute(
            update(WebhookDeliveryModel)
            .where(
                WebhookDeliveryModel.state.in_(("pending", "delivering")),
                WebhookDeliveryModel.available_at <= now,
                WebhookDeliveryModel.attempt_count >= WebhookDeliveryModel.max_attempts,
            )
            .values(state="failed", last_error_code="attempts_exhausted", lease_owner=None, lease_expires_at=None, claim_token=None, updated_at=now)
        )
        candidate = await self._session.scalar(
            select(WebhookDeliveryModel)
            .join(WebhookSubscriptionModel, WebhookSubscriptionModel.id == WebhookDeliveryModel.subscription_id)
            .where(
                or_(
                    and_(
                        WebhookDeliveryModel.state == "pending",
                        WebhookDeliveryModel.available_at <= now,
                    ),
                    and_(
                        WebhookDeliveryModel.state == "delivering",
                        WebhookDeliveryModel.lease_expires_at <= now,
                    ),
                ),
                WebhookSubscriptionModel.status == "active",
            )
            .order_by(WebhookDeliveryModel.available_at.asc(), WebhookDeliveryModel.id.asc())
            .with_for_update(of=WebhookDeliveryModel, skip_locked=True)
            .limit(1)
        )
        if candidate is None:
            return None
        token = uuid4()
        candidate.state = "delivering"
        candidate.attempt_count += 1
        candidate.lease_owner = worker_id.strip()
        candidate.lease_expires_at = now + timedelta(seconds=DELIVERY_LEASE_SECONDS)
        candidate.claim_token = token
        candidate.updated_at = now
        await self._session.flush()
        return DeliveryClaim(delivery_id=candidate.id, subscription_id=candidate.subscription_id, claim_token=token)

    async def deliver(self, claim: DeliveryClaim) -> bool:
        now = await self._session.scalar(select(func.now()))
        delivery = await self._session.scalar(
            select(WebhookDeliveryModel).where(
                WebhookDeliveryModel.id == claim.delivery_id,
                WebhookDeliveryModel.claim_token == claim.claim_token,
                WebhookDeliveryModel.subscription_id == claim.subscription_id,
                WebhookDeliveryModel.state == "delivering",
                WebhookDeliveryModel.lease_expires_at > now,
            )
        )
        if delivery is None:
            return False
        subscription = await self._session.get(WebhookSubscriptionModel, delivery.subscription_id)
        if subscription is None or subscription.status != "active":
            delivery.state = "failed"
            delivery.last_error_code = "subscriber_revoked"
            delivery.lease_owner = None
            delivery.lease_expires_at = None
            delivery.claim_token = None
            await self._session.flush()
            return False
        try:
            addresses = await _resolve_host_addresses(urlparse(subscription.url).hostname or "")
        except ValidationException:
            addresses = None
        if addresses is not None:
            internal = False
            for address in addresses:
                try:
                    parsed = ipaddress.ip_address(address)
                except ValueError:
                    internal = True
                    break
                if (
                    parsed.is_loopback
                    or parsed.is_unspecified
                    or parsed.is_link_local
                    or parsed.is_private
                    or parsed.is_reserved
                    or parsed.is_multicast
                ):
                    internal = True
                    break
            if internal:
                delivery.state = "failed"
                delivery.last_error_code = "internal_target"
                delivery.lease_owner = None
                delivery.lease_expires_at = None
                delivery.claim_token = None
                await self._session.flush()
                return False
        body = serialize_canonical(
            {
                "delivery_id": str(delivery.id),
                "event_type": delivery.event_type,
                "deduplication_key": delivery.deduplication_key,
                "payload": dict(delivery.payload or {}),
            }
        )
        timestamp = webhook_timestamp(now)
        secret_service = _webhook_secret_service()
        secret = _reveal_subscription_secret(subscription)
        if not secret:
            delivery.state = "failed"
            delivery.last_error_code = "secret_unavailable"
            delivery.lease_owner = None
            delivery.lease_expires_at = None
            delivery.claim_token = None
            await self._session.flush()
            return False
        if not subscription.secret_ciphertext and secret_service is not None:
            subscription.secret_ciphertext = secret_service.encrypt_secret(SecretValue(secret))
            subscription.secret_key_version = secret_service.active_key_version
            subscription.secret = ""
        signature = sign_webhook_payload(body, secret=secret, timestamp=timestamp)
        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
                    response = await client.post(
                        subscription.url,
                        content=body,
                        headers={
                            "content-type": "application/json",
                            "x-openknowledge-signature": f"t={timestamp},v1={signature}",
                            "x-openknowledge-delivery": str(delivery.id),
                        },
                    )
            else:
                response = await self._client.post(
                    subscription.url,
                    content=body,
                    headers={
                        "content-type": "application/json",
                        "x-openknowledge-signature": f"t={timestamp},v1={signature}",
                        "x-openknowledge-delivery": str(delivery.id),
                    },
                )
            delivery.last_status_code = response.status_code
            if 200 <= response.status_code < 300:
                delivery.state = "delivered"
                delivery.delivered_at = now
                delivery.lease_owner = None
                delivery.lease_expires_at = None
                delivery.claim_token = None
                await self._session.flush()
                return True
            if 400 <= response.status_code < 500:
                delivery.state = "failed"
                delivery.last_error_code = f"http_{response.status_code}"
                delivery.lease_owner = None
                delivery.lease_expires_at = None
                delivery.claim_token = None
                await self._session.flush()
                return False
            delivery.last_error_code = f"http_{response.status_code}"
        except Exception:
            logger.exception("Webhook delivery attempt failed", extra={"delivery_id": str(delivery.id)})
            delivery.last_error_code = "transport_error"
        if delivery.attempt_count >= delivery.max_attempts:
            delivery.state = "failed"
            delivery.lease_owner = None
            delivery.lease_expires_at = None
            delivery.claim_token = None
        else:
            delivery.state = "pending"
            delivery.available_at = now + delivery_backoff(delivery.attempt_count)
            delivery.lease_owner = None
            delivery.lease_expires_at = None
            delivery.claim_token = None
        await self._session.flush()
        return False


class WebhookDeliveryRunner:
    def __init__(
        self,
        session_factory,
        *,
        worker_id: str,
        idle_delay_seconds: float = 5.0,
        max_deliveries_per_cycle: int = 100,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("A delivery worker id is required")
        if idle_delay_seconds <= 0:
            raise ValueError("Delivery idle delay must be positive")
        if max_deliveries_per_cycle < 1:
            raise ValueError("Delivery cycle must contain at least one delivery")
        self._session_factory = session_factory
        self._worker_id = worker_id.strip()
        self._idle_delay_seconds = idle_delay_seconds
        self._max_deliveries_per_cycle = max_deliveries_per_cycle

    async def pump_once(self) -> UUID | None:
        async with self._session_factory.begin() as session:
            claim = await WebhookDeliveryService(session).claim_next(self._worker_id)
        if claim is None:
            return None
        try:
            async with self._session_factory.begin() as session:
                await WebhookDeliveryService(session).deliver(claim)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Webhook delivery iteration failed", extra={"delivery_id": str(claim.delivery_id)})
        return claim.delivery_id

    async def run_until_stopped(
        self,
        stop_event,
        *,
        idle_delay_seconds: float | None = None,
    ) -> None:
        delay = idle_delay_seconds if idle_delay_seconds is not None else self._idle_delay_seconds
        if delay <= 0:
            raise ValueError("Delivery idle delay must be positive")
        while not stop_event.is_set():
            try:
                delivered_any = False
                for _ in range(self._max_deliveries_per_cycle):
                    if stop_event.is_set():
                        break
                    result = await self.pump_once()
                    if result is None:
                        break
                    delivered_any = True
                if not delivered_any:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Webhook delivery cycle failed")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
