from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.audit_service import AuditService
from src.gateway.application.services.idempotency_service import IdempotencyService
from src.gateway.application.services.space_service import SpaceService
from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException
from src.gateway.domain.identity import Principal, PrincipalKind, SpaceRole
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import PendingAIActionModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace


@dataclass(frozen=True, slots=True)
class PendingAIAction:
    action_id: UUID
    tool_name: str
    expires_at: datetime


class AIManagementService:
    def __init__(self, session: AsyncSession, *, confirmation_lifetime: timedelta = timedelta(minutes=10)) -> None:
        self._session = session
        self._confirmation_lifetime = confirmation_lifetime
        self._audit = AuditService(AuditRepository(session))

    async def propose_space_archive(
        self,
        actor: Principal,
        *,
        space_id: str,
        expected_revision: int,
        request_id: str,
        now: datetime | None = None,
    ) -> PendingAIAction:
        current_time = now or datetime.now(timezone.utc)
        actor_id = self._member_id(actor)
        space = await self._session.get(Workspace, space_id)
        membership = await self._session.scalar(
            select(SpaceMembershipModel).where(
                SpaceMembershipModel.space_id == space_id,
                SpaceMembershipModel.member_id == actor_id,
            )
        )
        if (
            space is None
            or space.id == "global"
            or space.archived_at is not None
            or space.revision != expected_revision
            or membership is None
            or not is_allowed(
                AuthorizationContext(actor, SpaceRole(membership.role)),
                Action.SPACE_ARCHIVE,
            )
        ):
            raise AuthorizationException()
        command = {"space_id": space_id, "expected_revision": expected_revision}
        command_hash = IdempotencyService.request_hash(
            {"tool_name": "spaces.archive.v1", "arguments": command}
        )
        action = PendingAIActionModel(
            id=uuid4(),
            actor_member_id=actor_id,
            proposed_by_kind=actor.kind.value,
            tool_name="spaces.archive.v1",
            normalized_command=command,
            command_hash=command_hash,
            target_ids=[space_id],
            expected_revision=expected_revision,
            state="pending",
            expires_at=current_time + self._confirmation_lifetime,
            created_at=current_time,
        )
        self._session.add(action)
        self._audit.record(
            actor_member_id=actor_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="ai_action.proposed",
            resource_type="pending_ai_action",
            resource_id=str(action.id),
            details={"tool_name": action.tool_name, "target_ids": action.target_ids},
        )
        await self._session.flush()
        return PendingAIAction(action.id, action.tool_name, action.expires_at)

    async def confirm_and_execute(
        self,
        actor: Principal,
        action_id: UUID,
        *,
        request_id: str,
        now: datetime | None = None,
    ) -> PendingAIActionModel:
        if actor.kind is not PrincipalKind.SESSION:
            raise AuthorizationException()
        current_time = now or datetime.now(timezone.utc)
        actor_id = self._member_id(actor)
        action = await self._session.scalar(
            select(PendingAIActionModel)
            .where(PendingAIActionModel.id == action_id)
            .with_for_update()
        )
        if action is None or action.actor_member_id != actor_id:
            raise AuthorizationException()
        if action.state != "pending" or current_time >= action.expires_at:
            raise ResourceConflictException("The pending AI action is unavailable.")
        expected_hash = IdempotencyService.request_hash(
            {"tool_name": action.tool_name, "arguments": action.normalized_command}
        )
        if not compare_digest(expected_hash, action.command_hash):
            raise ResourceConflictException("The pending AI action is invalid.")
        if action.tool_name == "spaces.archive.v1":
            await self._execute_space_archive(actor, action, request_id=request_id)
        else:
            raise ResourceConflictException("The pending AI action tool is unavailable.")
        action.state = "executed"
        action.confirmed_by_member_id = actor_id
        action.confirmed_at = current_time
        action.consumed_at = current_time
        self._audit.record(
            actor_member_id=actor_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="ai_action.executed",
            resource_type="pending_ai_action",
            resource_id=str(action.id),
            details={"tool_name": action.tool_name, "target_ids": action.target_ids},
        )
        await self._session.flush()
        return action

    async def _execute_space_archive(
        self,
        actor: Principal,
        action: PendingAIActionModel,
        *,
        request_id: str,
    ) -> None:
        space_id = str(action.normalized_command["space_id"])
        expected_revision = int(action.normalized_command["expected_revision"])
        space = await self._session.scalar(
            select(Workspace).where(Workspace.id == space_id).with_for_update()
        )
        if space is None or space.revision != expected_revision:
            raise ResourceConflictException("The space changed after this action was proposed.")
        await SpaceService(self._session).archive(
            actor,
            space_id,
            request_id=request_id,
        )

    @staticmethod
    def _member_id(principal: Principal) -> UUID:
        if not principal.active:
            raise AuthorizationException()
        try:
            return UUID(principal.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
