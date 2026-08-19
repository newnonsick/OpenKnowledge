from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.audit_service import AuditService
from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException
from src.gateway.domain.identity import MemberStatus, Principal, SpaceRole
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace


@dataclass(frozen=True, slots=True)
class CreatedSpace:
    space_id: str
    name: str
    revision: int


class SpaceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(AuditRepository(session))

    async def create(
        self,
        actor: Principal,
        *,
        name: str,
        request_id: str,
        now: datetime | None = None,
    ) -> CreatedSpace:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 255:
            raise ValueError("Space name must contain between 1 and 255 characters")
        if not is_allowed(AuthorizationContext(actor), Action.SPACE_CREATE):
            raise AuthorizationException()
        try:
            member_id = UUID(actor.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
        member = await self._session.get(MemberModel, member_id)
        if member is None or member.status != MemberStatus.ACTIVE.value:
            raise AuthorizationException()
        space = Workspace(
            id=f"space-{uuid4().hex}",
            name=normalized_name,
            created_by_member_id=member_id,
            revision=1,
        )
        self._session.add(space)
        await self._session.flush()
        self._session.add(
            SpaceMembershipModel(
                id=uuid4(),
                space_id=space.id,
                member_id=member_id,
                role=SpaceRole.OWNER.value,
            )
        )
        self._audit.record(
            actor_member_id=member_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="space.created",
            resource_type="space",
            resource_id=space.id,
            details={"name": normalized_name},
        )
        await self._session.flush()
        return CreatedSpace(space.id, space.name, space.revision)

    async def set_membership(
        self,
        actor: Principal,
        space_id: str,
        target_member_id: UUID,
        *,
        role: SpaceRole | None,
        request_id: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(timezone.utc)
        space = await self._session.scalar(
            select(Workspace).where(Workspace.id == space_id).with_for_update()
        )
        if space is None or space.archived_at is not None:
            raise AuthorizationException()
        try:
            actor_id = UUID(actor.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
        actor_member = await self._session.get(MemberModel, actor_id)
        if actor_member is None or actor_member.status != MemberStatus.ACTIVE.value:
            raise AuthorizationException()
        actor_membership = await self._session.scalar(
            select(SpaceMembershipModel).where(
                SpaceMembershipModel.space_id == space_id,
                SpaceMembershipModel.member_id == actor_id,
            )
        )
        if (
            actor_membership is None
            or actor_membership.role != SpaceRole.OWNER.value
            or not is_allowed(
                AuthorizationContext(actor, SpaceRole.OWNER),
                Action.MEMBERSHIP_MANAGE,
            )
        ):
            raise AuthorizationException()
        target_membership = await self._session.scalar(
            select(SpaceMembershipModel).where(
                SpaceMembershipModel.space_id == space_id,
                SpaceMembershipModel.member_id == target_member_id,
            )
        )
        if role is None:
            if target_membership is None:
                raise AuthorizationException()
            await self._protect_owner_invariant(space_id, target_membership)
            await self._session.delete(target_membership)
        else:
            target = await self._session.get(MemberModel, target_member_id)
            if target is None or target.status != MemberStatus.ACTIVE.value:
                raise AuthorizationException()
            if target_membership is None:
                self._session.add(
                    SpaceMembershipModel(
                        id=uuid4(),
                        space_id=space_id,
                        member_id=target_member_id,
                        role=role.value,
                        updated_at=current_time,
                    )
                )
            else:
                if target_membership.role == role.value:
                    return
                if target_membership.role == SpaceRole.OWNER.value and role is not SpaceRole.OWNER:
                    await self._protect_owner_invariant(space_id, target_membership)
                target_membership.role = role.value
                target_membership.updated_at = current_time
        space.revision += 1
        self._audit.record(
            actor_member_id=actor_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="space.membership_changed",
            resource_type="space",
            resource_id=space_id,
            details={
                "target_member_id": str(target_member_id),
                "role": role.value if role is not None else None,
            },
        )
        await self._session.flush()

    async def archive(
        self,
        actor: Principal,
        space_id: str,
        *,
        request_id: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(timezone.utc)
        space = await self._session.scalar(
            select(Workspace).where(Workspace.id == space_id).with_for_update()
        )
        if space is None or space.id == "global" or space.archived_at is not None:
            raise AuthorizationException()
        try:
            actor_id = UUID(actor.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
        actor_member = await self._session.get(MemberModel, actor_id)
        if actor_member is None or actor_member.status != MemberStatus.ACTIVE.value:
            raise AuthorizationException()
        membership = await self._session.scalar(
            select(SpaceMembershipModel).where(
                SpaceMembershipModel.space_id == space_id,
                SpaceMembershipModel.member_id == actor_id,
            )
        )
        if membership is None or not is_allowed(
            AuthorizationContext(actor, SpaceRole(membership.role)),
            Action.SPACE_ARCHIVE,
        ):
            raise AuthorizationException()
        space.archived_at = current_time
        space.revision += 1
        self._audit.record(
            actor_member_id=actor_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="space.archived",
            resource_type="space",
            resource_id=space_id,
        )
        await self._session.flush()

    async def _protect_owner_invariant(
        self,
        space_id: str,
        membership: SpaceMembershipModel,
    ) -> None:
        if membership.role != SpaceRole.OWNER.value:
            return
        owner_count = int(
            await self._session.scalar(
                select(func.count())
                .select_from(SpaceMembershipModel)
                .where(
                    SpaceMembershipModel.space_id == space_id,
                    SpaceMembershipModel.role == SpaceRole.OWNER.value,
                )
            )
            or 0
        )
        if owner_count <= 1:
            raise ResourceConflictException("A space must retain at least one owner.")
