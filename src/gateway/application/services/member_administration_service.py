from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.security.tokens import SecretValue
from src.gateway.application.services.audit_service import AuditService
from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole, normalize_username
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import MFAFactorModel, MemberModel, PasswordCredentialModel, PersonalAPIKeyModel, SessionCredentialModel, SessionFamilyModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace


@dataclass(frozen=True, slots=True)
class CreatedMember:
    member_id: UUID
    username: str
    display_name: str
    temporary_password: SecretValue
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ResetPassword:
    member_id: UUID
    temporary_password: SecretValue
    expires_at: datetime


class MemberAdministrationService:
    def __init__(
        self,
        session: AsyncSession,
        password_service: PasswordService,
        *,
        step_up_window: timedelta = timedelta(minutes=10),
    ) -> None:
        self._session = session
        self._passwords = password_service
        self._step_up_window = step_up_window
        self._audit = AuditService(AuditRepository(session))

    async def create(
        self,
        actor: Principal,
        *,
        family_id: UUID,
        username: str,
        display_name: str,
        request_id: str,
        now: datetime | None = None,
    ) -> CreatedMember:
        current_time = now or datetime.now(timezone.utc)
        actor_id = await self._require_recent_super_admin(actor, family_id, current_time)
        normalized_username = normalize_username(username)
        clean_username = username.strip()
        clean_display_name = display_name.strip()
        if len(clean_username) > 255 or not clean_display_name or len(clean_display_name) > 255:
            raise ValueError("Invalid member identity")
        if await self._session.scalar(
            select(MemberModel.id).where(
                MemberModel.username_normalized == normalized_username
            )
        ):
            raise ResourceConflictException("Username is already in use.")
        temporary_password = SecretValue(secrets.token_urlsafe(24))
        password_hash = self._passwords.hash(
            temporary_password.reveal(),
            username=clean_username,
        )
        member = MemberModel(
            id=uuid4(),
            username=clean_username,
            username_normalized=normalized_username,
            display_name=clean_display_name,
            status=MemberStatus.PENDING.value,
            system_role=SystemRole.MEMBER.value,
            force_password_change=True,
        )
        expires_at = current_time + timedelta(hours=24)
        try:
            async with self._session.begin_nested():
                self._session.add(member)
                await self._session.flush()
        except IntegrityError as exc:
            raise ResourceConflictException("Username is already in use.") from exc
        self._session.add(
            PasswordCredentialModel(
                id=uuid4(),
                member_id=member.id,
                password_hash=password_hash,
                temporary=True,
                expires_at=expires_at,
            )
        )
        shared_exists = await self._session.scalar(
            select(Workspace.id).where(
                Workspace.id == "global",
                Workspace.archived_at.is_(None),
            )
        )
        if shared_exists:
            self._session.add(
                SpaceMembershipModel(
                    id=uuid4(),
                    space_id="global",
                    member_id=member.id,
                    role=SpaceRole.EDITOR.value,
                )
            )
        self._audit.record(
            actor_member_id=actor_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="member.created",
            resource_type="member",
            resource_id=str(member.id),
            details={
                "username": normalized_username,
                "temporary_expires_at": expires_at.isoformat(),
            },
        )
        await self._session.flush()
        return CreatedMember(
            member.id,
            clean_username,
            clean_display_name,
            temporary_password,
            expires_at,
        )

    async def update(
        self,
        actor: Principal,
        *,
        family_id: UUID,
        member_id: UUID,
        display_name: str,
        status: MemberStatus,
        system_role: SystemRole,
        request_id: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(timezone.utc)
        actor_id = await self._require_recent_super_admin(actor, family_id, current_time)
        member = await self._session.scalar(
            select(MemberModel).where(MemberModel.id == member_id).with_for_update()
        )
        if member is None:
            raise AuthorizationException()
        clean_display_name = display_name.strip()
        if not clean_display_name or len(clean_display_name) > 255:
            raise ValueError("Invalid member identity")
        active_admins = list(
            await self._session.scalars(
                select(MemberModel.id)
                .where(
                    MemberModel.status == MemberStatus.ACTIVE.value,
                    MemberModel.system_role == SystemRole.SUPER_ADMIN.value,
                )
                .with_for_update()
            )
        )
        removes_active_admin = (
            member.status == MemberStatus.ACTIVE.value
            and member.system_role == SystemRole.SUPER_ADMIN.value
            and (status is not MemberStatus.ACTIVE or system_role is not SystemRole.SUPER_ADMIN)
        )
        if removes_active_admin and len(active_admins) <= 1:
            raise ResourceConflictException("At least one active super admin is required.")
        if system_role is SystemRole.SUPER_ADMIN and member.system_role != SystemRole.SUPER_ADMIN.value:
            confirmed_mfa = await self._session.scalar(
                select(MFAFactorModel.id).where(
                    MFAFactorModel.member_id == member_id,
                    MFAFactorModel.confirmed_at.is_not(None),
                    MFAFactorModel.retired_at.is_(None),
                )
            )
            if confirmed_mfa is None:
                raise ResourceConflictException("Confirmed MFA is required before Super Admin promotion.")
        previous_status = member.status
        previous_role = member.system_role
        member.display_name = clean_display_name
        member.status = status.value
        member.system_role = system_role.value
        member.updated_at = current_time
        if status is MemberStatus.DISABLED:
            member.disabled_at = member.disabled_at or current_time
            await self._revoke_member_credentials(member_id, current_time)
        else:
            member.disabled_at = None
        self._audit.record(
            actor_member_id=actor_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="member.updated",
            resource_type="member",
            resource_id=str(member_id),
            details={
                "previous_status": previous_status,
                "status": status.value,
                "previous_system_role": previous_role,
                "system_role": system_role.value,
            },
        )
        await self._session.flush()

    async def reset_password(
        self,
        actor: Principal,
        *,
        family_id: UUID,
        member_id: UUID,
        request_id: str,
        now: datetime | None = None,
    ) -> ResetPassword:
        current_time = now or datetime.now(timezone.utc)
        actor_id = await self._require_recent_super_admin(actor, family_id, current_time)
        member = await self._session.scalar(
            select(MemberModel).where(MemberModel.id == member_id).with_for_update()
        )
        if member is None:
            raise AuthorizationException()
        current_credential = await self._session.scalar(
            select(PasswordCredentialModel)
            .where(
                PasswordCredentialModel.member_id == member_id,
                PasswordCredentialModel.retired_at.is_(None),
            )
            .with_for_update()
        )
        if current_credential is not None:
            current_credential.retired_at = current_time
        temporary_password = SecretValue(secrets.token_urlsafe(24))
        expires_at = current_time + timedelta(hours=24)
        self._session.add(
            PasswordCredentialModel(
                id=uuid4(),
                member_id=member_id,
                password_hash=self._passwords.hash(
                    temporary_password.reveal(),
                    username=member.username,
                ),
                temporary=True,
                expires_at=expires_at,
            )
        )
        member.force_password_change = True
        member.updated_at = current_time
        await self._revoke_member_credentials(member_id, current_time)
        self._audit.record(
            actor_member_id=actor_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="member.password_reset",
            resource_type="member",
            resource_id=str(member_id),
            details={"temporary_expires_at": expires_at.isoformat()},
        )
        await self._session.flush()
        return ResetPassword(member_id, temporary_password, expires_at)

    async def _revoke_member_credentials(self, member_id: UUID, current_time: datetime) -> None:
        family_ids = list(
            await self._session.scalars(
                select(SessionFamilyModel.id)
                .where(
                    SessionFamilyModel.member_id == member_id,
                    SessionFamilyModel.revoked_at.is_(None),
                )
                .with_for_update()
            )
        )
        if family_ids:
            await self._session.execute(
                update(SessionFamilyModel)
                .where(SessionFamilyModel.id.in_(family_ids))
                .values(revoked_at=current_time, revoke_reason="member_admin")
            )
            await self._session.execute(
                update(SessionCredentialModel)
                .where(
                    SessionCredentialModel.family_id.in_(family_ids),
                    SessionCredentialModel.revoked_at.is_(None),
                )
                .values(revoked_at=current_time)
            )
        await self._session.execute(
            update(PersonalAPIKeyModel)
            .where(
                PersonalAPIKeyModel.member_id == member_id,
                PersonalAPIKeyModel.status == "active",
            )
            .values(status="revoked", revoked_at=current_time)
        )

    async def _require_recent_super_admin(
        self,
        actor: Principal,
        family_id: UUID,
        current_time: datetime,
    ) -> UUID:
        if (
            actor.kind is not PrincipalKind.SESSION
            or not is_allowed(AuthorizationContext(actor), Action.MEMBER_ADMIN)
        ):
            raise AuthorizationException()
        try:
            actor_id = UUID(actor.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
        family = await self._session.scalar(
            select(SessionFamilyModel)
            .where(SessionFamilyModel.id == family_id)
            .with_for_update()
        )
        if (
            family is None
            or family.member_id != actor_id
            or family.revoked_at is not None
            or current_time >= family.idle_expires_at
            or current_time >= family.absolute_expires_at
            or family.last_step_up_at is None
            or current_time - family.last_step_up_at > self._step_up_window
        ):
            raise AuthorizationException()
        return actor_id
