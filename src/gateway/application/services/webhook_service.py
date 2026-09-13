from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.domain.exceptions import AuthenticationException, AuthorizationException
from src.gateway.infrastructure.persistence.ingestion_models import JobOutboxModel


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    id: str
    event_type: str
    job_id: str | None
    deduplication_key: str
    payload: dict
    created_at: str


def webhook_event_payload(event: JobOutboxModel, *, job_id: UUID | None = None) -> dict:
    body = {
        "id": str(event.id),
        "event_type": event.event_type,
        "job_id": str(job_id or event.job_id),
        "deduplication_key": event.deduplication_key,
        "payload": dict(event.payload or {}),
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }
    return body


def sign_webhook_payload(body: bytes, *, secret: str, timestamp: str) -> str:
    message = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_webhook_signature(signature: str, body: bytes, *, secret: str, timestamp: str) -> bool:
    expected = sign_webhook_payload(body, secret=secret, timestamp=timestamp)
    return hmac.compare_digest(signature, expected)


def webhook_event_from_row(event: JobOutboxModel) -> WebhookEvent:
    return WebhookEvent(
        id=str(event.id),
        event_type=event.event_type,
        job_id=str(event.job_id),
        deduplication_key=event.deduplication_key,
        payload=dict(event.payload or {}),
        created_at=event.created_at.isoformat() if event.created_at else "",
    )


async def list_webhook_events(
    session: AsyncSession,
    *,
    event_type: str | None = None,
    after_id: UUID | None = None,
    limit: int = 100,
    space_ids: tuple[str, ...] | None = None,
) -> list[JobOutboxModel]:
    from src.gateway.domain.exceptions import ItemNotFoundException

    from src.gateway.infrastructure.persistence.ingestion_models import IngestionJobModel

    query = (
        select(JobOutboxModel)
        .join(IngestionJobModel, IngestionJobModel.id == JobOutboxModel.job_id)
        .order_by(JobOutboxModel.id.asc())
        .limit(limit)
    )
    if space_ids is not None:
        query = query.where(IngestionJobModel.space_id.in_(space_ids))
    if event_type is not None:
        query = query.where(JobOutboxModel.event_type == event_type)
    if after_id is not None:
        anchor = await session.get(JobOutboxModel, after_id)
        if anchor is None:
            raise ItemNotFoundException("Webhook event cursor not found.")
        query = query.where(JobOutboxModel.id > anchor.id)
    return list(await session.scalars(query))


def webhook_timestamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return str(int(current.timestamp()))


def serialize_canonical(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
