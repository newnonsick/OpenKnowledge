from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from uuid import UUID, uuid4

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.security.tokens import SecretValue
from src.gateway.application.services.audit_service import AuditService
from src.gateway.domain.identity import MemberStatus, SpaceRole, SystemRole, normalize_username
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import MFAFactorModel, MemberModel, PasswordCredentialModel, SessionFamilyModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.identity_repository import IdentityRepository
from src.gateway.infrastructure.persistence.models import Workspace


class BootstrapAlreadyCompleted(Exception):
    pass


class BootstrapValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class BootstrapCredential:
    member_id: UUID
    temporary_password: SecretValue
    expires_at: datetime


class BootstrapService:
    def __init__(self, session: AsyncSession, password_service: PasswordService) -> None:
        self._session = session
        self._passwords = password_service
        self._identity = IdentityRepository(session)
        self._audit = AuditService(AuditRepository(session))

    async def create_first_super_admin(
        self,
        *,
        username: str,
        display_name: str,
        request_id: str,
        now: datetime | None = None,
    ) -> BootstrapCredential:
        current_time = now or datetime.now(timezone.utc)
        if self._session.bind and self._session.bind.dialect.name == "postgresql":
            await self._session.execute(text("SELECT pg_advisory_xact_lock(7046029254386353131)"))
        if await self._identity.super_admin_count() != 0:
            raise BootstrapAlreadyCompleted("A Super Admin already exists")
        try:
            normalized = normalize_username(username)
        except ValueError as exc:
            raise BootstrapValidationError(str(exc)) from exc
        clean_display_name = display_name.strip()
        if len(normalized) > 255 or not clean_display_name or len(clean_display_name) > 255:
            raise BootstrapValidationError("Invalid member identity")
        temporary_password = SecretValue(secrets.token_urlsafe(24))
        member = MemberModel(
            id=uuid4(),
            username=username.strip(),
            username_normalized=normalized,
            display_name=clean_display_name,
            status=MemberStatus.PENDING.value,
            system_role=SystemRole.SUPER_ADMIN.value,
            force_password_change=True,
        )
        self._identity.add_member(member)
        await self._session.flush()
        expires_at = current_time + timedelta(hours=24)
        self._session.add(
            PasswordCredentialModel(
                id=uuid4(),
                member_id=member.id,
                password_hash=self._passwords.hash(temporary_password.reveal(), username=username),
                temporary=True,
                expires_at=expires_at,
            )
        )
        shared = await self._session.scalar(select(Workspace).where(Workspace.id == "global").with_for_update())
        if shared is None:
            shared = Workspace(
                id="global",
                name="Family Shared",
                created_by_member_id=member.id,
            )
            self._session.add(shared)
        else:
            shared.name = "Family Shared"
            if shared.created_by_member_id is None:
                shared.created_by_member_id = member.id
        await self._session.flush()
        self._session.add(
            SpaceMembershipModel(
                id=uuid4(),
                space_id="global",
                member_id=member.id,
                role=SpaceRole.OWNER.value,
            )
        )
        self._audit.record(
            actor_member_id=member.id,
            actor_kind="bootstrap",
            request_id=request_id,
            action="member.bootstrap_super_admin",
            resource_type="member",
            resource_id=str(member.id),
            details={"temporary_expires_at": expires_at.isoformat()},
        )
        await self._session.flush()
        return BootstrapCredential(member.id, temporary_password, expires_at)

    async def recover_super_admin(
        self,
        *,
        username: str,
        request_id: str,
        now: datetime | None = None,
    ) -> BootstrapCredential:
        current_time = now or datetime.now(timezone.utc)
        if self._session.bind and self._session.bind.dialect.name == "postgresql":
            await self._session.execute(text("SELECT pg_advisory_xact_lock(7046029254386353131)"))
        try:
            member = await self._identity.get_member_by_username(username, for_update=True)
        except ValueError as exc:
            raise BootstrapValidationError(str(exc)) from exc
        if member is None or member.system_role != SystemRole.SUPER_ADMIN.value:
            raise BootstrapValidationError("Eligible Super Admin not found")
        temporary_password = SecretValue(secrets.token_urlsafe(24))
        expires_at = current_time + timedelta(hours=24)
        await self._identity.replace_password(
            PasswordCredentialModel(
                id=uuid4(),
                member_id=member.id,
                password_hash=self._passwords.hash(temporary_password.reveal(), username=member.username),
                temporary=True,
                expires_at=expires_at,
            ),
            current_time,
        )
        member.status = MemberStatus.PENDING.value
        member.force_password_change = True
        member.disabled_at = None
        member.updated_at = current_time
        await self._session.execute(
            update(SessionFamilyModel)
            .where(SessionFamilyModel.member_id == member.id, SessionFamilyModel.revoked_at.is_(None))
            .values(revoked_at=current_time, revoke_reason="host_recovery")
        )
        await self._session.execute(
            update(MFAFactorModel)
            .where(MFAFactorModel.member_id == member.id, MFAFactorModel.retired_at.is_(None))
            .values(retired_at=current_time)
        )
        self._audit.record(
            actor_member_id=member.id,
            actor_kind="host_recovery",
            request_id=request_id,
            action="member.recover_super_admin",
            resource_type="member",
            resource_id=str(member.id),
            details={"temporary_expires_at": expires_at.isoformat()},
        )
        await self._session.flush()
        return BootstrapCredential(member.id, temporary_password, expires_at)
