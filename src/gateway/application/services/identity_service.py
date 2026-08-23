from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.passwords import PasswordService, normalize_password
from src.gateway.application.security.tokens import SecretValue
from src.gateway.application.security.totp import MFASecretService
from src.gateway.application.services.audit_service import AuditService
from src.gateway.domain.exceptions import AuthenticationException, ValidationException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import MFAFactorModel, MFARecoveryCodeModel, MemberModel, PasswordCredentialModel, SessionFamilyModel
from src.gateway.infrastructure.persistence.identity_repository import IdentityRepository


@dataclass(frozen=True, slots=True)
class PasswordAuthentication:
    principal: Principal
    temporary_credential: bool


@dataclass(frozen=True, slots=True)
class TotpEnrollment:
    factor_id: UUID
    secret: SecretValue


class IdentityService:
    def __init__(
        self,
        session: AsyncSession,
        password_service: PasswordService,
        mfa_service: MFASecretService,
    ) -> None:
        self._session = session
        self._passwords = password_service
        self._mfa = mfa_service
        self._identity = IdentityRepository(session)
        self._audit = AuditService(AuditRepository(session))

    async def authenticate_password(
        self,
        username: str,
        password: str,
        *,
        now: datetime | None = None,
    ) -> PasswordAuthentication:
        current_time = now or datetime.now(timezone.utc)
        member = await self._identity.get_member_by_username(username, for_update=True)
        if member is None or member.status == MemberStatus.DISABLED.value:
            raise AuthenticationException("Invalid username or password.")
        credential = await self._identity.current_password(member.id)
        if credential is None:
            raise AuthenticationException("Invalid username or password.")
        if credential.expires_at is not None and credential.expires_at <= current_time:
            raise AuthenticationException("Invalid username or password.")
        if not self._passwords.verify(credential.password_hash, password):
            raise AuthenticationException("Invalid username or password.")
        if self._passwords.needs_rehash(credential.password_hash):
            credential.password_hash = self._passwords.hash(password, username=member.username)
        restricted = member.force_password_change or member.status != MemberStatus.ACTIVE.value
        return PasswordAuthentication(
            principal=Principal(
                subject_id=str(member.id),
                kind=PrincipalKind.SESSION,
                system_role=SystemRole(member.system_role),
                scopes=frozenset({"*"}),
                active=True,
                restricted=restricted,
            ),
            temporary_credential=credential.temporary,
        )

    async def verify_totp_login(self, member_id: UUID, code: str) -> bool:
        if not code:
            return False
        factor = await self._session.scalar(
            select(MFAFactorModel).where(
                MFAFactorModel.member_id == member_id,
                MFAFactorModel.confirmed_at.is_not(None),
                MFAFactorModel.retired_at.is_(None),
            )
        )
        if factor is None:
            return False
        return self._mfa.verify_totp(
            factor.secret_ciphertext,
            code,
            key_version=factor.encryption_key_version,
        )

    async def change_password(
        self,
        member_id: UUID,
        *,
        new_password: str,
        confirmation: str,
        request_id: str,
        now: datetime | None = None,
    ) -> None:
        if normalize_password(new_password) != normalize_password(confirmation):
            raise ValidationException("Password confirmation does not match")
        current_time = now or datetime.now(timezone.utc)
        member = await self._identity.get_member(member_id, for_update=True)
        if member is None or member.status == MemberStatus.DISABLED.value:
            raise AuthenticationException("Authentication required.")
        try:
            password_hash = self._passwords.hash(new_password, username=member.username)
        except ValueError as exc:
            raise ValidationException(str(exc)) from exc
        credential = PasswordCredentialModel(
            id=uuid4(),
            member_id=member.id,
            password_hash=password_hash,
            temporary=False,
            expires_at=None,
        )
        await self._identity.replace_password(credential, current_time)
        member.force_password_change = False
        if member.system_role != SystemRole.SUPER_ADMIN.value:
            member.status = MemberStatus.ACTIVE.value
        member.updated_at = current_time
        await self._session.execute(
            update(SessionFamilyModel)
            .where(SessionFamilyModel.member_id == member.id, SessionFamilyModel.revoked_at.is_(None))
            .values(revoked_at=current_time, revoke_reason="password_changed")
        )
        self._audit.record(
            actor_member_id=member.id,
            actor_kind="member",
            request_id=request_id,
            action="credential.password_changed",
            resource_type="member",
            resource_id=str(member.id),
        )
        await self._session.flush()

    async def begin_totp_enrollment(
        self,
        member_id: UUID,
        *,
        request_id: str,
        now: datetime | None = None,
    ) -> TotpEnrollment:
        current_time = now or datetime.now(timezone.utc)
        member = await self._identity.get_member(member_id, for_update=True)
        if member is None or member.status == MemberStatus.DISABLED.value or member.force_password_change:
            raise AuthenticationException("Authentication state does not allow MFA enrollment.")
        await self._session.execute(
            update(MFAFactorModel)
            .where(
                MFAFactorModel.member_id == member_id,
                MFAFactorModel.retired_at.is_(None),
                MFAFactorModel.confirmed_at.is_(None),
            )
            .values(retired_at=current_time)
        )
        secret = self._mfa.new_totp_secret()
        encryption_key_version = self._mfa.active_key_version
        factor = MFAFactorModel(
            id=uuid4(),
            member_id=member_id,
            factor_type="totp",
            secret_ciphertext=self._mfa.encrypt_secret(secret, key_version=encryption_key_version),
            encryption_key_version=encryption_key_version,
        )
        self._session.add(factor)
        self._audit.record(
            actor_member_id=member.id,
            actor_kind="member",
            request_id=request_id,
            action="mfa.enrollment_started",
            resource_type="mfa_factor",
            resource_id=str(factor.id),
            details={"encryption_key_version": encryption_key_version},
        )
        await self._session.flush()
        return TotpEnrollment(factor.id, secret)

    async def confirm_totp_enrollment(
        self,
        member_id: UUID,
        factor_id: UUID,
        code: str,
        *,
        request_id: str,
        now: datetime | None = None,
        recovery_count: int = 10,
    ) -> tuple[SecretValue, ...]:
        current_time = now or datetime.now(timezone.utc)
        factor = await self._session.scalar(
            select(MFAFactorModel)
            .where(
                MFAFactorModel.id == factor_id,
                MFAFactorModel.member_id == member_id,
                MFAFactorModel.retired_at.is_(None),
                MFAFactorModel.confirmed_at.is_(None),
            )
            .with_for_update()
        )
        if factor is None or not self._mfa.verify_totp(
            factor.secret_ciphertext,
            code,
            key_version=factor.encryption_key_version,
        ):
            raise AuthenticationException("Invalid authentication code.")
        member = await self._identity.get_member(member_id, for_update=True)
        if member is None or member.force_password_change:
            raise AuthenticationException("Authentication state does not allow MFA enrollment.")
        factor.confirmed_at = current_time
        await self._session.execute(
            update(MFAFactorModel)
            .where(
                MFAFactorModel.member_id == member_id,
                MFAFactorModel.id != factor.id,
                MFAFactorModel.retired_at.is_(None),
            )
            .values(retired_at=current_time)
        )
        recovery_codes = tuple(self._mfa.new_recovery_code() for _ in range(recovery_count))
        for recovery_code in recovery_codes:
            self._session.add(
                MFARecoveryCodeModel(
                    id=uuid4(),
                    factor_id=factor.id,
                    code_digest=self._mfa.hash_recovery_code(
                        recovery_code.reveal(),
                        key_version=factor.encryption_key_version,
                    ),
                )
            )
        member.status = MemberStatus.ACTIVE.value
        member.updated_at = current_time
        await self._session.execute(
            update(SessionFamilyModel)
            .where(SessionFamilyModel.member_id == member.id, SessionFamilyModel.revoked_at.is_(None))
            .values(revoked_at=current_time, revoke_reason="mfa_enrolled")
        )
        self._audit.record(
            actor_member_id=member.id,
            actor_kind="member",
            request_id=request_id,
            action="mfa.enrollment_confirmed",
            resource_type="mfa_factor",
            resource_id=str(factor.id),
            details={"recovery_code_count": recovery_count},
        )
        await self._session.flush()
        return recovery_codes

    async def consume_recovery_code(
        self,
        member_id: UUID,
        code: str,
        *,
        request_id: str,
        now: datetime | None = None,
    ) -> bool:
        current_time = now or datetime.now(timezone.utc)
        rows = (
            await self._session.execute(
                select(MFARecoveryCodeModel, MFAFactorModel.encryption_key_version)
                .join(MFAFactorModel, MFAFactorModel.id == MFARecoveryCodeModel.factor_id)
                .where(
                    MFAFactorModel.member_id == member_id,
                    MFAFactorModel.confirmed_at.is_not(None),
                    MFAFactorModel.retired_at.is_(None),
                    MFARecoveryCodeModel.consumed_at.is_(None),
                )
                .with_for_update()
            )
        ).all()
        matched = next(
            (
                row
                for row, key_version in rows
                if self._mfa.verify_recovery_code(code, row.code_digest, key_version=key_version)
            ),
            None,
        )
        if matched is None:
            return False
        matched.consumed_at = current_time
        self._audit.record(
            actor_member_id=member_id,
            actor_kind="member",
            request_id=request_id,
            action="mfa.recovery_code_consumed",
            resource_type="member",
            resource_id=str(member_id),
        )
        await self._session.flush()
        return True
