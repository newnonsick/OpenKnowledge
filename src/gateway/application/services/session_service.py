from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.tokens import OpaqueTokenCodec, SecretValue
from src.gateway.application.services.audit_service import AuditService
from src.gateway.domain.exceptions import AuthenticationException, CSRFException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SessionCredentialModel, SessionFamilyModel
from src.gateway.observability import increment_metric


class RefreshStatus(StrEnum):
    ROTATED = "rotated"
    ALREADY_ROTATED = "already_rotated"
    REUSE_DETECTED = "reuse_detected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    access_lifetime: timedelta = timedelta(minutes=15)
    idle_lifetime: timedelta = timedelta(days=7)
    absolute_lifetime: timedelta = timedelta(days=30)
    concurrent_rotation_grace: timedelta = timedelta(seconds=5)


@dataclass(frozen=True, slots=True)
class SessionSecrets:
    family_id: UUID
    access_token: SecretValue
    refresh_token: SecretValue
    csrf_token: SecretValue
    access_expires_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True, slots=True)
class RefreshRotation:
    status: RefreshStatus
    session: SessionSecrets | None = None


class SessionService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        policy: SessionPolicy | None = None,
        tokens: OpaqueTokenCodec | None = None,
    ) -> None:
        self._session = session
        self._policy = policy or SessionPolicy()
        self._tokens = tokens or OpaqueTokenCodec()
        self._audit = AuditService(AuditRepository(session))

    async def issue(
        self,
        principal: Principal,
        *,
        now: datetime | None = None,
        step_up_at: datetime | None = None,
    ) -> SessionSecrets:
        if not principal.active:
            raise AuthenticationException("Invalid session.")
        current_time = now or datetime.now(timezone.utc)
        if step_up_at is not None and step_up_at > current_time:
            raise ValueError("Step-up time cannot be in the future")
        try:
            member_id = UUID(principal.subject_id)
        except ValueError as exc:
            raise AuthenticationException("Invalid session.") from exc
        member = await self._session.get(MemberModel, member_id)
        if (
            member is None
            or member.status == MemberStatus.DISABLED.value
            or member.system_role != principal.system_role.value
            or (member.status != MemberStatus.ACTIVE.value and not principal.restricted)
        ):
            raise AuthenticationException("Invalid session.")
        family_id = uuid4()
        access = self._tokens.issue()
        refresh = self._tokens.issue()
        csrf = self._tokens.issue()
        access_expires_at = current_time + self._policy.access_lifetime
        idle_expires_at = current_time + self._policy.idle_lifetime
        absolute_expires_at = current_time + self._policy.absolute_lifetime
        family = SessionFamilyModel(
            id=family_id,
            member_id=member_id,
            created_at=current_time,
            last_activity_at=current_time,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
            csrf_token_digest=self._tokens.digest(csrf),
            last_step_up_at=step_up_at,
        )
        self._session.add(family)
        await self._session.flush()
        self._session.add_all(
            [
                SessionCredentialModel(
                    id=uuid4(),
                    family_id=family_id,
                    credential_type="access",
                    token_digest=self._tokens.digest(access),
                    issued_at=current_time,
                    expires_at=access_expires_at,
                ),
                SessionCredentialModel(
                    id=uuid4(),
                    family_id=family_id,
                    credential_type="refresh",
                    token_digest=self._tokens.digest(refresh),
                    issued_at=current_time,
                    expires_at=idle_expires_at,
                ),
            ]
        )
        await self._session.flush()
        return SessionSecrets(
            family_id,
            access,
            refresh,
            csrf,
            access_expires_at,
            idle_expires_at,
            absolute_expires_at,
        )

    async def rotate_refresh(
        self,
        raw_refresh_token: str,
        *,
        now: datetime | None = None,
        request_id: str,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> RefreshRotation:
        current_time = now or datetime.now(timezone.utc)
        token_digest = self._tokens.digest(raw_refresh_token)
        candidate = await self._session.scalar(
            select(SessionCredentialModel)
            .where(
                SessionCredentialModel.token_digest == token_digest,
                SessionCredentialModel.credential_type == "refresh",
            )
        )
        if candidate is None:
            raise AuthenticationException("Invalid session.")
        family = await self._session.scalar(
            select(SessionFamilyModel)
            .where(SessionFamilyModel.id == candidate.family_id)
            .with_for_update()
        )
        if family is None or family.revoked_at is not None:
            raise AuthenticationException("Invalid session.")
        if require_csrf and (
            not csrf_token
            or family.csrf_token_digest is None
            or not self._tokens.verify(csrf_token, family.csrf_token_digest)
        ):
            raise CSRFException()
        credential = await self._session.scalar(
            select(SessionCredentialModel)
            .where(
                SessionCredentialModel.id == candidate.id,
                SessionCredentialModel.family_id == family.id,
                SessionCredentialModel.token_digest == token_digest,
                SessionCredentialModel.credential_type == "refresh",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if credential is None:
            raise AuthenticationException("Invalid session.")
        if current_time >= credential.expires_at or current_time >= family.idle_expires_at or current_time >= family.absolute_expires_at:
            family.revoked_at = current_time
            family.revoke_reason = "expired"
            await self._revoke_credentials(family.id, current_time)
            return RefreshRotation(RefreshStatus.EXPIRED)
        if credential.used_at is not None:
            if current_time - credential.used_at <= self._policy.concurrent_rotation_grace:
                return RefreshRotation(RefreshStatus.ALREADY_ROTATED)
            family.revoked_at = current_time
            family.revoke_reason = "refresh_reuse"
            await self._revoke_credentials(family.id, current_time)
            self._audit.record(
                actor_member_id=family.member_id,
                actor_kind="member",
                request_id=request_id,
                action="session.refresh_reuse_detected",
                resource_type="session_family",
                resource_id=str(family.id),
                outcome="denied",
            )
            increment_metric("gateway_auth_events_total", event="token_reuse", outcome="detected")
            return RefreshRotation(RefreshStatus.REUSE_DETECTED)
        member = await self._session.get(MemberModel, family.member_id)
        if member is None or member.status == MemberStatus.DISABLED.value:
            family.revoked_at = current_time
            family.revoke_reason = "member_inactive"
            await self._revoke_credentials(family.id, current_time)
            increment_metric("gateway_auth_events_total", event="token_reuse", outcome="member_inactive")
            return RefreshRotation(RefreshStatus.REUSE_DETECTED)
        access = self._tokens.issue()
        refresh = self._tokens.issue()
        csrf = self._tokens.issue()
        access_expires_at = min(current_time + self._policy.access_lifetime, family.absolute_expires_at)
        refresh_expires_at = min(family.idle_expires_at, family.absolute_expires_at)
        access_credential = SessionCredentialModel(
            id=uuid4(),
            family_id=family.id,
            credential_type="access",
            token_digest=self._tokens.digest(access),
            issued_at=current_time,
            expires_at=access_expires_at,
        )
        refresh_credential = SessionCredentialModel(
            id=uuid4(),
            family_id=family.id,
            credential_type="refresh",
            token_digest=self._tokens.digest(refresh),
            issued_at=current_time,
            expires_at=refresh_expires_at,
        )
        await self._session.execute(
            update(SessionCredentialModel)
            .where(
                SessionCredentialModel.family_id == family.id,
                SessionCredentialModel.credential_type == "access",
                SessionCredentialModel.revoked_at.is_(None),
            )
            .values(revoked_at=current_time)
        )
        self._session.add_all([access_credential, refresh_credential])
        await self._session.flush()
        credential.used_at = current_time
        credential.replaced_by_id = refresh_credential.id
        family.csrf_token_digest = self._tokens.digest(csrf)
        await self._session.flush()
        return RefreshRotation(
            RefreshStatus.ROTATED,
            SessionSecrets(
                family.id,
                access,
                refresh,
                csrf,
                access_expires_at,
                family.idle_expires_at,
                family.absolute_expires_at,
            ),
        )

    async def resolve_access(
        self,
        raw_access_token: str,
        *,
        now: datetime | None = None,
    ) -> Principal:
        current_time = now or datetime.now(timezone.utc)
        credential = await self._session.scalar(
            select(SessionCredentialModel).where(
                SessionCredentialModel.token_digest == self._tokens.digest(raw_access_token),
                SessionCredentialModel.credential_type == "access",
            )
        )
        if credential is None or credential.revoked_at is not None or current_time >= credential.expires_at:
            raise AuthenticationException("Invalid session.")
        family = await self._session.get(SessionFamilyModel, credential.family_id)
        if (
            family is None
            or family.revoked_at is not None
            or current_time >= family.idle_expires_at
            or current_time >= family.absolute_expires_at
        ):
            raise AuthenticationException("Invalid session.")
        member = await self._session.get(MemberModel, family.member_id)
        if member is None or member.status == MemberStatus.DISABLED.value:
            raise AuthenticationException("Invalid session.")
        return Principal(
            subject_id=str(member.id),
            kind=PrincipalKind.SESSION,
            system_role=SystemRole(member.system_role),
            scopes=frozenset({"*"}),
            credential_id=str(credential.id),
            restricted=member.force_password_change or member.status != MemberStatus.ACTIVE.value,
        )

    async def verify_csrf(self, family_id: UUID, candidate: str) -> bool:
        family = await self._session.get(SessionFamilyModel, family_id)
        if family is None or family.revoked_at is not None or family.csrf_token_digest is None:
            return False
        return self._tokens.verify(candidate, family.csrf_token_digest)

    async def record_activity(
        self,
        family_id: UUID,
        *,
        meaningful: bool,
        now: datetime | None = None,
    ) -> None:
        if not meaningful:
            return
        current_time = now or datetime.now(timezone.utc)
        family = await self._session.scalar(
            select(SessionFamilyModel).where(SessionFamilyModel.id == family_id).with_for_update()
        )
        if family is None or family.revoked_at is not None:
            raise AuthenticationException("Invalid session.")
        if current_time >= family.idle_expires_at or current_time >= family.absolute_expires_at:
            raise AuthenticationException("Invalid session.")
        family.last_activity_at = current_time
        family.idle_expires_at = min(current_time + self._policy.idle_lifetime, family.absolute_expires_at)
        await self._session.execute(
            update(SessionCredentialModel)
            .where(
                SessionCredentialModel.family_id == family.id,
                SessionCredentialModel.credential_type == "refresh",
                SessionCredentialModel.used_at.is_(None),
                SessionCredentialModel.revoked_at.is_(None),
            )
            .values(expires_at=family.idle_expires_at)
        )

    async def record_step_up(
        self,
        family_id: UUID,
        *,
        request_id: str,
        now: datetime | None = None,
    ) -> datetime:
        current_time = now or datetime.now(timezone.utc)
        family = await self._session.scalar(
            select(SessionFamilyModel).where(SessionFamilyModel.id == family_id).with_for_update()
        )
        if (
            family is None
            or family.revoked_at is not None
            or current_time >= family.idle_expires_at
            or current_time >= family.absolute_expires_at
        ):
            raise AuthenticationException("Invalid session.")
        family.last_step_up_at = current_time
        self._audit.record(
            actor_member_id=family.member_id,
            actor_kind="session",
            request_id=request_id,
            action="session.step_up_completed",
            resource_type="session_family",
            resource_id=str(family.id),
        )
        await self._session.flush()
        return current_time + timedelta(minutes=10)

    async def revoke_family(
        self,
        family_id: UUID,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        current_time = now or datetime.now(timezone.utc)
        family = await self._session.scalar(
            select(SessionFamilyModel).where(SessionFamilyModel.id == family_id).with_for_update()
        )
        if family is None:
            return
        family.revoked_at = family.revoked_at or current_time
        family.revoke_reason = family.revoke_reason or reason
        await self._revoke_credentials(family.id, current_time)

    async def _revoke_credentials(self, family_id: UUID, at: datetime) -> None:
        await self._session.execute(
            update(SessionCredentialModel)
            .where(
                SessionCredentialModel.family_id == family_id,
                SessionCredentialModel.revoked_at.is_(None),
            )
            .values(revoked_at=at)
        )
