from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.services.webhook_delivery_service import (
    WebhookDeliveryService,
    WebhookSubscriptionService,
)
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.database import get_db_session
from src.gateway.infrastructure.persistence.ingestion_models import (
    WebhookDeliveryModel,
    WebhookSubscriptionModel,
)
from src.gateway.infrastructure.persistence.models import KnowledgeItem as KnowledgeItemModel
from src.gateway.infrastructure.persistence.models import KnowledgeRevision as KnowledgeRevisionModel
from src.gateway.application.services.permission_service import require_profile_route
from src.gateway.presentation.authorization import require_scope
from src.gateway.presentation.request_context import get_request_id
from src.gateway.presentation.schemas.management_responses import (
    MANAGEMENT_ERROR_RESPONSES,
    ContractModel,
)


router = APIRouter(prefix="/api/v1", tags=["Webhooks"], responses=MANAGEMENT_ERROR_RESPONSES)


class WebhookSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=2048)
    secret: str = Field(min_length=1, max_length=512)
    event_filter: list[str] = Field(default_factory=list, max_length=32)


class WebhookSubscription(ContractModel):
    id: str
    space_id: str
    url: str
    status: str
    created_at: str | None


class WebhookSubscriptionList(ContractModel):
    items: list[WebhookSubscription]


class WebhookDelivery(ContractModel):
    id: str
    event_type: str
    state: str
    attempt_count: int
    last_error_code: str | None
    delivered_at: str | None


class WebhookDeliveryList(ContractModel):
    items: list[WebhookDelivery]


class ReviewReminder(ContractModel):
    id: str
    space_id: str
    title: str
    updated_at: str
    age_days: int
    remedy: str


class ReviewReminderList(ContractModel):
    items: list[ReviewReminder]


def _subscription_payload(subscription: WebhookSubscriptionModel) -> dict:
    return {
        "id": str(subscription.id),
        "space_id": subscription.space_id,
        "url": subscription.url,
        "status": subscription.status,
        "created_at": subscription.created_at.isoformat() if subscription.created_at else None,
    }


@router.post("/webhook-subscriptions", status_code=201, response_model=WebhookSubscription)
async def register_webhook_subscription(
    payload: WebhookSubscriptionRequest,
    request: Request,
    principal: Principal = Depends(require_scope("spaces:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "webhook.write")
    subscription = await WebhookSubscriptionService(session).register(
        principal,
        space_id=payload.space_id,
        url=payload.url,
        secret=payload.secret,
        event_filter=tuple(payload.event_filter),
        request_id=get_request_id(request),
    )
    return _subscription_payload(subscription)


@router.get("/webhook-subscriptions", response_model=WebhookSubscriptionList)
async def list_webhook_subscriptions(
    space_id: str = Query(min_length=1, max_length=64),
    principal: Principal = Depends(require_scope("spaces:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "webhook.read")
    subscriptions = await WebhookSubscriptionService(session).list_for_space(
        principal, space_id=space_id
    )
    return {"items": [_subscription_payload(item) for item in subscriptions]}


@router.delete("/webhook-subscriptions/{subscription_id}", status_code=204)
async def revoke_webhook_subscription(
    subscription_id: UUID,
    principal: Principal = Depends(require_scope("spaces:write")),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    require_profile_route(principal, "webhook.write")
    await WebhookSubscriptionService(session).revoke(principal, subscription_id=subscription_id)
    return None


@router.get("/webhook-deliveries", response_model=WebhookDeliveryList)
async def list_webhook_deliveries(
    subscription_id: UUID = Query(),
    principal: Principal = Depends(require_scope("spaces:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "webhook.read")
    subscription = await session.get(WebhookSubscriptionModel, subscription_id)
    if subscription is None:
        raise AuthorizationException()
    spaces = await AuthorizationService(session).effective_space_ids(
        UUID(principal.subject_id), requested={subscription.space_id}, principal=principal
    )
    if subscription.space_id not in spaces:
        raise AuthorizationException()
    rows = list(
        await session.scalars(
            select(WebhookDeliveryModel)
            .where(WebhookDeliveryModel.subscription_id == subscription_id)
            .order_by(WebhookDeliveryModel.created_at.desc())
            .limit(100)
        )
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "event_type": row.event_type,
                "state": row.state,
                "attempt_count": row.attempt_count,
                "last_error_code": row.last_error_code,
                "delivered_at": row.delivered_at.isoformat() if row.delivered_at else None,
            }
            for row in rows
        ]
    }


@router.get("/knowledge/review-reminders", response_model=ReviewReminderList)
async def list_review_reminders(
    space_id: str | None = Query(default=None, min_length=1, max_length=64),
    older_than_days: int | None = Query(default=None, ge=1, le=3650),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.stale")
    try:
        member_id = UUID(principal.subject_id)
    except ValueError as exc:
        raise AuthorizationException() from exc
    effective = await AuthorizationService(session).effective_space_ids(
        member_id,
        requested={space_id} if space_id is not None else None,
        principal=principal,
    )
    if space_id is not None:
        scoped = (space_id,) if space_id in effective else ()
    else:
        scoped = effective
    if not scoped:
        return {"items": []}
    threshold = older_than_days if older_than_days is not None else get_settings().gateway.stale_after_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=threshold)
    observed = datetime.now(timezone.utc)
    rows = list(
        await session.scalars(
            select(KnowledgeItemModel)
            .outerjoin(
                KnowledgeRevisionModel,
                KnowledgeRevisionModel.id == KnowledgeItemModel.current_revision_id,
            )
            .where(
                KnowledgeItemModel.is_deleted.is_(False),
                KnowledgeItemModel.archived_at.is_(None),
                KnowledgeItemModel.workspace_id.in_(scoped),
                KnowledgeItemModel.updated_at < cutoff,
            )
            .order_by(KnowledgeItemModel.updated_at.asc(), KnowledgeItemModel.id.asc())
            .limit(limit)
        )
    )
    return {
        "items": [
            {
                "id": str(item.id),
                "space_id": item.workspace_id,
                "title": item.title,
                "updated_at": item.updated_at.isoformat(),
                "age_days": max(0, int((observed - item.updated_at).total_seconds() // 86400)),
                "remedy": "reviewed",
            }
            for item in rows
        ]
    }


async def enqueue_subscription_event(
    session: AsyncSession,
    *,
    space_id: str,
    event_type: str,
    deduplication_key: str,
    payload: dict,
) -> int:
    subscriptions = list(
        await session.scalars(
            select(WebhookSubscriptionModel).where(
                WebhookSubscriptionModel.space_id == space_id,
                WebhookSubscriptionModel.status == "active",
            )
        )
    )
    service = WebhookDeliveryService(session)
    enqueued = 0
    for subscription in subscriptions:
        if subscription.event_filter and event_type not in set(subscription.event_filter):
            continue
        await service.enqueue(
            subscription_id=subscription.id,
            event_type=event_type,
            deduplication_key=f"{subscription.id}:{deduplication_key}",
            payload=payload,
        )
        enqueued += 1
    return enqueued
