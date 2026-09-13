from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.services.idempotency_service import IdempotencyService, ReservationStatus
from src.gateway.application.services.knowledge_management_service import KnowledgeManagementService
from src.gateway.application.use_cases.context import UseCaseContext, UseCaseOutcome, actor_id, require_mutation_tools
from src.gateway.domain.authorization import Action
from src.gateway.domain.entities import KnowledgeItem as DomainKnowledgeItem
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException, ValidationException


@dataclass(frozen=True, slots=True)
class CreateKnowledgeCommand:
    space_id: str
    title: str
    content: str
    tags: tuple[str, ...] = ()
    lifecycle_status: str = "accepted"
    origin: str | None = None
    source_detail: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class UpdateKnowledgeCommand:
    item_id: UUID
    expected_version: int
    title: str
    content: str
    tags: tuple[str, ...] = ()
    change_summary: str | None = None
    review_note: str | None = None
    expires_at: datetime | None = None
    update_expires_at: bool = False


@dataclass(frozen=True, slots=True)
class TransitionKnowledgeCommand:
    item_id: UUID
    to_status: str
    expected_version: int
    review_note: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteKnowledgeCommand:
    item_id: UUID
    expected_version: int


class KnowledgeCommands:
    def __init__(
        self,
        session: AsyncSession,
        *,
        knowledge_factory=None,
        idempotency_factory=None,
    ) -> None:
        self._session = session
        self._knowledge_factory = knowledge_factory or KnowledgeManagementService
        self._idempotency_factory = idempotency_factory or IdempotencyService

    async def create(
        self,
        ctx: UseCaseContext,
        command: CreateKnowledgeCommand,
        *,
        operation: str = "knowledge.create",
    ) -> UseCaseOutcome[DomainKnowledgeItem]:
        require_mutation_tools(ctx.policy)
        key = self._write_key(ctx)
        payload = {
            "space_id": command.space_id,
            "title": command.title,
            "content": command.content,
            "tags": list(command.tags),
            "lifecycle_status": command.lifecycle_status,
            "origin": command.origin,
            "source_detail": command.source_detail,
            "expires_at": command.expires_at.isoformat() if command.expires_at else None,
        }
        reservation = await self._idempotency(ctx, operation, key, payload)
        service = self._knowledge_factory(self._session)
        if reservation.status is ReservationStatus.REPLAY:
            if not reservation.resource_ids:
                raise ResourceConflictException()
            existing = await service.get(UUID(reservation.resource_ids[0]))
            if existing is None:
                raise ResourceConflictException()
            return UseCaseOutcome(existing, True)
        created = await service.create(
            ctx.principal,
            space_id=command.space_id,
            title=command.title.strip(),
            content=command.content,
            tags=[tag.strip() for tag in command.tags if tag.strip()],
            request_id=ctx.request_id,
            lifecycle_status=command.lifecycle_status,
            origin=command.origin,
            source_detail=command.source_detail,
            expires_at=command.expires_at,
        )
        await self._complete(reservation.record_id, response_status=201, resource_ids=[str(created.id)])
        return UseCaseOutcome(created, False)

    async def get(self, ctx: UseCaseContext, item_id: UUID) -> DomainKnowledgeItem:
        item = await self._knowledge_factory(self._session).get(item_id)
        if item is None:
            raise AuthorizationException()
        await AuthorizationService(self._session).authorize_space(
            ctx.principal, item.workspace_id, Action.CONTENT_READ
        )
        return item

    async def update(
        self,
        ctx: UseCaseContext,
        command: UpdateKnowledgeCommand,
        *,
        operation: str = "knowledge.update",
    ) -> UseCaseOutcome[DomainKnowledgeItem]:
        require_mutation_tools(ctx.policy)
        key = self._write_key(ctx)
        payload = {
            "item_id": str(command.item_id),
            "expected_version": command.expected_version,
            "title": command.title,
            "content": command.content,
            "tags": list(command.tags),
            "change_summary": command.change_summary,
            "review_note": command.review_note,
            "expires_at": command.expires_at.isoformat() if command.expires_at else None,
            "update_expires_at": command.update_expires_at,
        }
        reservation = await self._idempotency(ctx, operation, key, payload)
        service = self._knowledge_factory(self._session)
        if reservation.status is ReservationStatus.REPLAY:
            replay_id = UUID(reservation.resource_ids[0]) if reservation.resource_ids else command.item_id
            current = await service.get(replay_id)
            if current is None:
                raise ResourceConflictException()
            return UseCaseOutcome(current, True)
        updated = await service.update(
            ctx.principal,
            command.item_id,
            expected_version=command.expected_version,
            title=command.title.strip(),
            content=command.content,
            tags=[tag.strip() for tag in command.tags if tag.strip()],
            change_summary=command.change_summary,
            request_id=ctx.request_id,
            review_note=command.review_note,
            expires_at=command.expires_at,
            update_expires_at=command.update_expires_at,
        )
        await self._complete(reservation.record_id, response_status=200, resource_ids=[str(command.item_id)])
        return UseCaseOutcome(updated, False)

    async def transition(
        self,
        ctx: UseCaseContext,
        command: TransitionKnowledgeCommand,
        *,
        operation: str = "knowledge.transition",
    ) -> UseCaseOutcome[tuple[DomainKnowledgeItem, str]]:
        require_mutation_tools(ctx.policy)
        key = self._write_key(ctx)
        payload = {
            "item_id": str(command.item_id),
            "to_status": command.to_status,
            "expected_version": command.expected_version,
            "review_note": command.review_note,
        }
        reservation = await self._idempotency(ctx, operation, key, payload)
        service = self._knowledge_factory(self._session)
        if reservation.status is ReservationStatus.REPLAY:
            replay_id = UUID(reservation.resource_ids[0]) if reservation.resource_ids else command.item_id
            current = await service.get(replay_id)
            if current is None:
                raise ResourceConflictException()
            await AuthorizationService(self._session).authorize_space(
                ctx.principal, current.workspace_id, Action.CONTENT_WRITE
            )
            replayed_from = reservation.resource_ids[1] if len(reservation.resource_ids) > 1 else ""
            return UseCaseOutcome((current, replayed_from), True)
        transitioned, from_status = await service.transition(
            ctx.principal,
            command.item_id,
            to_status=command.to_status,
            expected_version=command.expected_version,
            review_note=command.review_note,
            request_id=ctx.request_id,
        )
        await self._complete(
            reservation.record_id, response_status=200, resource_ids=[str(command.item_id), from_status]
        )
        return UseCaseOutcome((transitioned, from_status), False)

    async def delete(
        self,
        ctx: UseCaseContext,
        command: DeleteKnowledgeCommand,
        *,
        operation: str = "knowledge.delete",
    ) -> UseCaseOutcome[None]:
        require_mutation_tools(ctx.policy)
        key = self._write_key(ctx)
        payload = {"item_id": str(command.item_id), "expected_version": command.expected_version}
        reservation = await self._idempotency(ctx, operation, key, payload)
        if reservation.status is not ReservationStatus.REPLAY:
            await self._knowledge_factory(self._session).delete(
                ctx.principal,
                command.item_id,
                expected_version=command.expected_version,
                request_id=ctx.request_id,
            )
            await self._complete(
                reservation.record_id, response_status=204, resource_ids=[str(command.item_id)]
            )
        return UseCaseOutcome(None, reservation.status is ReservationStatus.REPLAY)

    async def _idempotency(self, ctx: UseCaseContext, operation: str, key: str, payload: dict):
        reservation = await self._idempotency_factory(self._session).reserve(
            actor_id=ctx.principal.subject_id,
            operation=operation,
            idempotency_key=key,
            payload=payload,
        )
        if reservation.status is ReservationStatus.IN_PROGRESS:
            raise ResourceConflictException("An identical request is still in progress.")
        return reservation

    async def _complete(self, record_id: UUID, *, response_status: int, resource_ids: list[str]) -> None:
        await self._idempotency_factory(self._session).complete(
            record_id,
            response_status=response_status,
            resource_ids=resource_ids,
        )

    @staticmethod
    def _write_key(ctx: UseCaseContext) -> str:
        if not ctx.idempotency_key:
            raise ValidationException("An idempotency key is required.")
        return ctx.idempotency_key


def member_id(ctx: UseCaseContext) -> UUID:
    return actor_id(ctx.principal)
