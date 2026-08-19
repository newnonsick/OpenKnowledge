from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.domain.identity import SystemRole, normalize_username
from src.gateway.infrastructure.persistence.identity_models import MemberModel, PasswordCredentialModel


class IdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def member_count(self) -> int:
        return int(await self._session.scalar(select(func.count()).select_from(MemberModel)) or 0)

    async def super_admin_count(self) -> int:
        return int(
            await self._session.scalar(
                select(func.count()).select_from(MemberModel).where(
                    MemberModel.system_role == SystemRole.SUPER_ADMIN.value
                )
            )
            or 0
        )

    async def get_member_by_username(self, username: str, *, for_update: bool = False) -> MemberModel | None:
        query = select(MemberModel).where(MemberModel.username_normalized == normalize_username(username))
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    async def get_member(self, member_id: UUID, *, for_update: bool = False) -> MemberModel | None:
        query = select(MemberModel).where(MemberModel.id == member_id)
        if for_update:
            query = query.with_for_update()
        return await self._session.scalar(query)

    def add_member(self, member: MemberModel) -> None:
        self._session.add(member)

    async def current_password(self, member_id: UUID) -> PasswordCredentialModel | None:
        return await self._session.scalar(
            select(PasswordCredentialModel).where(
                PasswordCredentialModel.member_id == member_id,
                PasswordCredentialModel.retired_at.is_(None),
            )
        )

    async def replace_password(self, credential: PasswordCredentialModel, retired_at: datetime) -> None:
        await self._session.execute(
            update(PasswordCredentialModel)
            .where(
                PasswordCredentialModel.member_id == credential.member_id,
                PasswordCredentialModel.retired_at.is_(None),
            )
            .values(retired_at=retired_at)
        )
        self._session.add(credential)
