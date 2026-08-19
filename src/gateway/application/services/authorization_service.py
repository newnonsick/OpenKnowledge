from __future__ import annotations

from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import MemberStatus, Principal, SpaceRole
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace


class AuthorizationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def effective_space_ids(
        self,
        member_id: UUID,
        *,
        requested: set[str] | frozenset[str] | None = None,
    ) -> tuple[str, ...]:
        query = (
            select(SpaceMembershipModel.space_id)
            .join(MemberModel, MemberModel.id == SpaceMembershipModel.member_id)
            .join(Workspace, Workspace.id == SpaceMembershipModel.space_id)
            .where(
                SpaceMembershipModel.member_id == member_id,
                MemberModel.status == MemberStatus.ACTIVE.value,
                Workspace.archived_at.is_(None),
            )
        )
        if requested is not None:
            if not requested:
                return ()
            query = query.where(SpaceMembershipModel.space_id.in_(requested))
        query = query.order_by(
            case((SpaceMembershipModel.space_id == "global", 0), else_=1),
            SpaceMembershipModel.space_id,
        )
        return tuple(await self._session.scalars(query))

    async def authorize_space(
        self,
        principal: Principal,
        space_id: str,
        action: Action,
    ) -> SpaceRole:
        try:
            member_id = UUID(principal.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
        row = await self._session.execute(
            select(SpaceMembershipModel.role)
            .join(MemberModel, MemberModel.id == SpaceMembershipModel.member_id)
            .join(Workspace, Workspace.id == SpaceMembershipModel.space_id)
            .where(
                SpaceMembershipModel.space_id == space_id,
                SpaceMembershipModel.member_id == member_id,
                MemberModel.status == MemberStatus.ACTIVE.value,
                Workspace.archived_at.is_(None),
            )
        )
        role_value = row.scalar_one_or_none()
        if role_value is None:
            raise AuthorizationException()
        role = SpaceRole(role_value)
        if not is_allowed(AuthorizationContext(principal, role), action):
            raise AuthorizationException()
        return role
