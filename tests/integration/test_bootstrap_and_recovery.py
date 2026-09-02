from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from sqlalchemy import select

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.security.totp import MFASecretService
from src.gateway.application.services.bootstrap_service import (
    BootstrapAlreadyCompleted,
    BootstrapService,
    BootstrapValidationError,
)
from src.gateway.application.services.identity_service import IdentityService
from src.gateway.application.services.session_service import SessionService
from src.gateway.domain.exceptions import ValidationException
from src.gateway.domain.identity import MemberStatus, SpaceRole
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MemberModel, PasswordCredentialModel, SessionFamilyModel, SpaceMembershipModel
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_bootstrap_first_login_totp_and_recovery_state_machine() -> None:
    password_service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)
    mfa_service = MFASecretService.generate()
    now = datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            bootstrap = BootstrapService(session, password_service)
            with pytest.raises(BootstrapValidationError, match="Username is required"):
                await bootstrap.create_first_super_admin(
                    username="   ",
                    display_name="Invalid",
                    request_id="invalid-bootstrap",
                    now=now,
                )
            assert (await session.scalars(select(MemberModel))).all() == []
            with pytest.raises(BootstrapValidationError, match="Username is required"):
                await bootstrap.recover_super_admin(
                    username="   ",
                    request_id="invalid-recovery",
                    now=now,
                )
            issued = await bootstrap.create_first_super_admin(
                username="Älice",
                display_name="Alice",
                request_id="bootstrap-request",
                now=now,
            )
            temporary_password = issued.temporary_password.reveal()
            assert issued.expires_at > now
            with pytest.raises(BootstrapAlreadyCompleted):
                await bootstrap.create_first_super_admin(
                    username="other",
                    display_name="Other",
                    request_id="duplicate-bootstrap",
                    now=now,
                )

        async with factory.begin() as session:
            member = await session.scalar(select(MemberModel).where(MemberModel.id == issued.member_id))
            credential = await session.scalar(
                select(PasswordCredentialModel).where(PasswordCredentialModel.member_id == issued.member_id)
            )
            membership = await session.scalar(
                select(SpaceMembershipModel).where(SpaceMembershipModel.member_id == issued.member_id)
            )
            audit = await session.scalar(
                select(AuditEventModel).where(AuditEventModel.request_id == "bootstrap-request")
            )
            assert member is not None and member.status == MemberStatus.PENDING.value
            assert credential is not None and credential.temporary is True
            assert temporary_password not in credential.password_hash
            assert membership is not None and membership.space_id == "global"
            assert membership.role == SpaceRole.OWNER.value
            assert temporary_password not in str(audit.details)

            identity = IdentityService(session, password_service, mfa_service)
            authenticated = await identity.authenticate_password("äLICE", temporary_password, now=now)
            assert authenticated.principal.restricted is True
            with pytest.raises(ValidationException, match="confirmation"):
                await identity.change_password(
                    issued.member_id,
                    new_password="a new permanent password",
                    confirmation="does not match password",
                    now=now,
                    request_id="password-mismatch",
                )
            await identity.change_password(
                issued.member_id,
                new_password="A-New-Pässword-934-Enough!",
                confirmation="A-New-Pa\u0308ssword-934-Enough!",
                now=now,
                request_id="password-change",
            )
            password_authentication = await identity.authenticate_password(
                "älice",
                "A-New-Pässword-934-Enough!",
                now=now,
            )
            restricted_session = await SessionService(session).issue(
                password_authentication.principal,
                now=now,
            )
            enrollment = await identity.begin_totp_enrollment(
                issued.member_id,
                now=now,
                request_id="mfa-begin",
            )
            assert enrollment.provisioning_uri.startswith("otpauth://totp/OpenKnowledge:")
            assert enrollment.secret.reveal() in enrollment.provisioning_uri
            code = pyotp.TOTP(enrollment.secret.reveal()).now()
            recovery_codes = await identity.confirm_totp_enrollment(
                issued.member_id,
                enrollment.factor_id,
                code,
                now=now,
                request_id="mfa-confirm",
            )
            assert len(recovery_codes) == 10
            assert len({value.reveal() for value in recovery_codes}) == 10

        async with factory.begin() as session:
            identity = IdentityService(session, password_service, mfa_service)
            authenticated = await identity.authenticate_password("Älice", "A-New-Pässword-934-Enough!", now=now)
            assert authenticated.principal.restricted is False
            family = await session.get(SessionFamilyModel, restricted_session.family_id)
            assert family is not None and family.revoke_reason == "mfa_enrolled"
            with pytest.raises(Exception, match="Invalid session"):
                await SessionService(session).resolve_access(
                    restricted_session.access_token.reveal(),
                    now=now,
                )
            first_code = recovery_codes[0].reveal()
            assert await identity.consume_recovery_code(
                issued.member_id,
                first_code,
                now=now,
                request_id="recovery-success",
            ) is True
            assert await identity.consume_recovery_code(
                issued.member_id,
                first_code,
                now=now,
                request_id="recovery-reuse",
            ) is False
            await identity.begin_totp_enrollment(
                issued.member_id,
                now=now,
                request_id="mfa-replacement-abandoned",
            )
            assert await identity.consume_recovery_code(
                issued.member_id,
                recovery_codes[1].reveal(),
                now=now,
                request_id="old-factor-still-active",
            ) is True
            recovered = await BootstrapService(session, password_service).recover_super_admin(
                username="älice",
                request_id="host-recovery",
                now=now + timedelta(hours=1),
            )

        async with factory.begin() as session:
            identity = IdentityService(session, password_service, mfa_service)
            with pytest.raises(Exception, match="Invalid username or password"):
                await identity.authenticate_password("älice", "A-New-Pässword-934-Enough!", now=now)
            recovered_login = await identity.authenticate_password(
                "älice",
                recovered.temporary_password.reveal(),
                now=now + timedelta(hours=1),
            )
            assert recovered_login.principal.restricted is True


async def test_expired_temporary_password_fails_with_generic_error() -> None:
    password_service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)
    now = datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            issued = await BootstrapService(session, password_service).create_first_super_admin(
                username="admin",
                display_name="Admin",
                request_id="bootstrap-expiry",
                now=now,
            )
        async with factory.begin() as session:
            identity = IdentityService(session, password_service, MFASecretService.generate())
            with pytest.raises(Exception, match="Invalid username or password"):
                await identity.authenticate_password(
                    "admin",
                    issued.temporary_password.reveal(),
                    now=issued.expires_at + timedelta(seconds=1),
                )
