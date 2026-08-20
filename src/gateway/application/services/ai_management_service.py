from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.audit_service import AuditService
from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.services.idempotency_service import IdempotencyService
from src.gateway.application.services.ingestion_job_service import IngestionJobService
from src.gateway.application.services.knowledge_management_service import KnowledgeManagementService
from src.gateway.application.services.runtime_settings_service import RuntimeSettingsService, RuntimeSettingsValues
from src.gateway.application.services.space_service import SpaceService
from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException
from src.gateway.domain.identity import Principal, PrincipalKind, SpaceRole
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import MemberModel, PendingAIActionModel, SessionCredentialModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import IngestionJobModel
from src.gateway.infrastructure.persistence.models import KnowledgeItem, KnowledgeRevision, Workspace


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
        return await self.propose(
            actor,
            tool_name="spaces.archive.v1",
            command={"space_id": space_id, "expected_revision": expected_revision},
            request_id=request_id,
            now=now,
        )

    async def propose(
        self,
        actor: Principal,
        *,
        tool_name: str,
        command: dict,
        request_id: str,
        now: datetime | None = None,
    ) -> PendingAIAction:
        current_time = now or datetime.now(timezone.utc)
        actor_id = self._member_id(actor)
        target_ids, expected_revision = await self._validate_proposal(actor, tool_name, command)
        command_hash = IdempotencyService.request_hash(
            {"tool_name": tool_name, "arguments": command}
        )
        action = PendingAIActionModel(
            id=uuid4(),
            actor_member_id=actor_id,
            proposed_by_kind=actor.kind.value,
            tool_name=tool_name,
            normalized_command=command,
            command_hash=command_hash,
            target_ids=target_ids,
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

    async def _validate_proposal(
        self,
        actor: Principal,
        tool_name: str,
        command: dict,
    ) -> tuple[list[str], int | None]:
        if tool_name == "spaces.archive.v1":
            space_id = str(command["space_id"])
            expected = int(command["expected_revision"])
            space = await self._session.get(Workspace, space_id)
            membership = await self._session.scalar(
                select(SpaceMembershipModel).where(
                    SpaceMembershipModel.space_id == space_id,
                    SpaceMembershipModel.member_id == self._member_id(actor),
                )
            )
            if (
                space is None
                or space.id == "global"
                or space.archived_at is not None
                or space.revision != expected
                or membership is None
                or not is_allowed(AuthorizationContext(actor, SpaceRole(membership.role)), Action.SPACE_ARCHIVE)
            ):
                raise AuthorizationException()
            return [space_id], expected
        if tool_name == "spaces.members.set.v1":
            space_id = str(command["space_id"])
            expected = int(command["expected_space_revision"])
            space = await self._session.get(Workspace, space_id)
            if space is None or space.archived_at is not None or space.revision != expected:
                raise AuthorizationException()
            role = await AuthorizationService(self._session).authorize_space(actor, space_id, Action.MEMBERSHIP_MANAGE)
            if role is not SpaceRole.OWNER:
                raise AuthorizationException()
            target_member_id = UUID(str(command["member_id"]))
            target = await self._session.get(MemberModel, target_member_id)
            if command.get("role") is None:
                target_membership = await self._session.scalar(
                    select(SpaceMembershipModel).where(
                        SpaceMembershipModel.space_id == space_id,
                        SpaceMembershipModel.member_id == target_member_id,
                    )
                )
                if target_membership is None:
                    raise AuthorizationException()
            elif target is None or target.status != "active":
                raise AuthorizationException()
            return [space_id, str(target_member_id)], expected
        if tool_name == "knowledge.archive.v1":
            item_id = UUID(str(command["item_id"]))
            expected = int(command["expected_version"])
            row = (
                await self._session.execute(
                    select(KnowledgeItem, KnowledgeRevision)
                    .join(KnowledgeRevision, KnowledgeRevision.id == KnowledgeItem.current_revision_id)
                    .where(KnowledgeItem.id == item_id, KnowledgeItem.is_deleted.is_(False))
                )
            ).one_or_none()
            if row is None or row[1].version != expected:
                raise AuthorizationException()
            await AuthorizationService(self._session).authorize_space(actor, row[0].workspace_id, Action.CONTENT_WRITE)
            return [str(item_id)], expected
        if tool_name in {"ingestion_jobs.cancel.v1", "ingestion_jobs.retry.v1"}:
            job_id = UUID(str(command["job_id"]))
            expected_state = str(command["expected_state"])
            job = await self._session.get(IngestionJobModel, job_id)
            if job is None or job.state != expected_state:
                raise AuthorizationException()
            await AuthorizationService(self._session).authorize_space(actor, job.space_id, Action.CONTENT_WRITE)
            if tool_name.endswith("cancel.v1") and job.state in {"succeeded", "failed", "cancelled"}:
                raise ResourceConflictException("The ingestion job cannot be cancelled in its current state.")
            if tool_name.endswith("retry.v1") and job.state not in {"failed", "cancelled"}:
                raise ResourceConflictException("The ingestion job cannot be retried in its current state.")
            return [str(job_id)], None
        if tool_name == "settings.propose.v1":
            if not is_allowed(AuthorizationContext(actor), Action.MEMBER_ADMIN):
                raise AuthorizationException()
            base_revision = int(command["base_revision"])
            active = await RuntimeSettingsService(self._session).active()
            if active.revision != base_revision:
                raise ResourceConflictException("The runtime settings changed before this proposal was created.")
            RuntimeSettingsValues.model_validate(command["values"])
            return ["runtime_settings"], base_revision
        raise ResourceConflictException("The pending AI action tool is unavailable.")

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
        elif action.tool_name == "spaces.members.set.v1":
            await self._execute_membership(actor, action, request_id=request_id)
        elif action.tool_name == "knowledge.archive.v1":
            await self._execute_knowledge_archive(actor, action, request_id=request_id)
        elif action.tool_name in {"ingestion_jobs.cancel.v1", "ingestion_jobs.retry.v1"}:
            await self._execute_ingestion_job(actor, action)
        elif action.tool_name == "settings.propose.v1":
            await self._execute_settings_proposal(actor, action, request_id=request_id, now=current_time)
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

    async def _execute_membership(
        self,
        actor: Principal,
        action: PendingAIActionModel,
        *,
        request_id: str,
    ) -> None:
        command = action.normalized_command
        space_id = str(command["space_id"])
        expected = int(command["expected_space_revision"])
        space = await self._session.scalar(
            select(Workspace).where(Workspace.id == space_id).with_for_update()
        )
        if space is None or space.revision != expected:
            raise ResourceConflictException("The space changed after this action was proposed.")
        role_value = command.get("role")
        await SpaceService(self._session).set_membership(
            actor,
            space_id,
            UUID(str(command["member_id"])),
            role=SpaceRole(role_value) if role_value is not None else None,
            request_id=request_id,
        )

    async def _execute_knowledge_archive(
        self,
        actor: Principal,
        action: PendingAIActionModel,
        *,
        request_id: str,
    ) -> None:
        command = action.normalized_command
        await KnowledgeManagementService(self._session).delete(
            actor,
            UUID(str(command["item_id"])),
            expected_version=int(command["expected_version"]),
            request_id=request_id,
        )

    async def _execute_ingestion_job(
        self,
        actor: Principal,
        action: PendingAIActionModel,
    ) -> None:
        command = action.normalized_command
        job_id = UUID(str(command["job_id"]))
        job = await self._session.scalar(
            select(IngestionJobModel).where(IngestionJobModel.id == job_id).with_for_update()
        )
        if job is None or job.state != str(command["expected_state"]):
            raise ResourceConflictException("The ingestion job changed after this action was proposed.")
        await AuthorizationService(self._session).authorize_space(actor, job.space_id, Action.CONTENT_WRITE)
        if action.tool_name == "ingestion_jobs.cancel.v1":
            await IngestionJobService(self._session).request_cancellation(job_id)
        else:
            await IngestionJobService(self._session).request_retry(job_id)

    async def _execute_settings_proposal(
        self,
        actor: Principal,
        action: PendingAIActionModel,
        *,
        request_id: str,
        now: datetime,
    ) -> None:
        if actor.credential_id is None:
            raise AuthorizationException()
        try:
            credential_id = UUID(actor.credential_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
        credential = await self._session.get(SessionCredentialModel, credential_id)
        if credential is None or credential.revoked_at is not None:
            raise AuthorizationException()
        command = action.normalized_command
        draft = await RuntimeSettingsService(self._session).create_draft(
            actor,
            family_id=credential.family_id,
            base_revision=int(command["base_revision"]),
            values=RuntimeSettingsValues.model_validate(command["values"]),
            reason=str(command["reason"]),
            request_id=request_id,
            now=now,
        )
        action.target_ids = [str(draft.id)]

    @staticmethod
    def _member_id(principal: Principal) -> UUID:
        if not principal.active:
            raise AuthorizationException()
        try:
            return UUID(principal.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
