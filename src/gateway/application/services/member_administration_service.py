from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.security.tokens import SecretValue
from src.gateway.application.services.audit_service import AuditService
from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole, normalize_username
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import MemberModel, PasswordCredentialModel, SessionFamilyModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace


@dataclass(frozen=True, slots=True)
class CreatedMember:
    member_id: UUID
    username: str
    display_name: str
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
