from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.security.tokens import SecretValue
from src.gateway.application.security.totp import MFASecretService
from src.gateway.application.services.api_key_service import APIKeyService, CreatedAPIKey
from src.gateway.application.services.identity_service import IdentityService
from src.gateway.application.services.login_throttle_service import LoginThrottleService, request_client_ip
from src.gateway.application.services.session_service import RefreshStatus, SessionSecrets, SessionService
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import AuthenticationException, CSRFException, MfaCodeRequiredException, RateLimitException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.database import get_db_session, get_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SessionCredentialModel
from src.gateway.presentation.api_keys import configured_api_key_codec
from src.gateway.presentation.request_context import get_request_id
from src.gateway.presentation.schemas.management_responses import (
    MANAGEMENT_ERROR_RESPONSES,
    SessionAuthentication,
    SessionRefresh,
    SessionStepUp,
    SignOut,
    TotpConfirmation,
    TotpEnrollment,
)


router = APIRouter(
    prefix="/api/v1/auth",
    tags=["Management authentication"],
    responses=MANAGEMENT_ERROR_RESPONSES,
)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)
    totp_code: str | None = Field(default=None, min_length=6, max_length=8)
    recovery_code: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_second_factor(self) -> "LoginRequest":
        if self.totp_code and self.recovery_code:
            raise ValueError("Provide one authentication factor")
        return self


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str | None = Field(default=None, min_length=1, max_length=128)
    current_totp_code: str | None = Field(default=None, min_length=6, max_length=8)
    recovery_code: str | None = Field(default=None, min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    confirmation: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_second_factor(self) -> "PasswordChangeRequest":
        if self.current_totp_code and self.recovery_code:
            raise ValueError("Provide one authentication factor")
        return self


class StepUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=128)
    totp_code: str | None = Field(default=None, min_length=6, max_length=8)
    recovery_code: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_second_factor(self) -> "StepUpRequest":
        if self.totp_code and self.recovery_code:
            raise ValueError("Provide one authentication factor")
        return self


class TotpEnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str | None = Field(default=None, min_length=1, max_length=128)
    current_totp_code: str | None = Field(default=None, min_length=6, max_length=8)
    recovery_code: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_second_factor(self) -> "TotpEnrollRequest":
        if self.current_totp_code and self.recovery_code:
            raise ValueError("Provide one authentication factor")
        return self


class TotpConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factor_id: UUID
    code: str = Field(min_length=6, max_length=8)
    current_password: str | None = Field(default=None, min_length=1, max_length=128)
    current_totp_code: str | None = Field(default=None, min_length=6, max_length=8)
    recovery_code: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_second_factor(self) -> "TotpConfirmRequest":
        if self.current_totp_code and self.recovery_code:
            raise ValueError("Provide one authentication factor")
        return self


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _identity_service(session: AsyncSession) -> IdentityService:
    gateway = get_settings().gateway
    if not gateway.mfa_encryption_keys:
        raise RuntimeError("MFA encryption keys are unavailable")
    mfa = MFASecretService(
        {
            version: SecretValue(value)
            for version, value in gateway.mfa_encryption_keys.items()
        },
        active_key_version=gateway.active_mfa_encryption_key_version,
    )
    return IdentityService(session, PasswordService(), mfa)


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None or not principal.active or principal.kind is not PrincipalKind.SESSION:
        raise AuthenticationException("Authentication required.")
    return principal


def _member_id(principal: Principal) -> UUID:
    try:
        return UUID(principal.subject_id)
    except ValueError as exc:
        raise AuthenticationException("Authentication required.") from exc


def _credential_id(principal: Principal) -> UUID:
    if principal.credential_id is None:
        raise AuthenticationException("Authentication required.")
    try:
        return UUID(principal.credential_id)
    except ValueError as exc:
        raise AuthenticationException("Authentication required.") from exc


async def _verify_current_credentials(
    identity: IdentityService,
    member: MemberModel,
    *,
    password: str | None,
    totp_code: str | None,
    recovery_code: str | None,
    request_id: str,
) -> None:
    if not password:
        raise AuthenticationException("Invalid current credentials.")
    authenticated = await identity.authenticate_password(member.username, password)
    if authenticated.principal.subject_id != str(member.id) or authenticated.principal.restricted:
        raise AuthenticationException("Invalid current credentials.")
    if member.system_role != SystemRole.SUPER_ADMIN.value:
        return
    if totp_code:
        verified = await identity.verify_totp_login(member.id, totp_code)
    elif recovery_code:
        verified = await identity.consume_recovery_code(
            member.id,
            recovery_code,
            request_id=request_id,
        )
    else:
        verified = False
    if not verified:
        raise AuthenticationException("Invalid current credentials.")


def _verify_origin(request: Request) -> None:
    if request.headers.get("Origin") not in get_settings().gateway.csrf_allowed_origins:
        raise CSRFException()


def _apply_session_cookies(response: Response, secrets: SessionSecrets) -> None:
    now = datetime.now(timezone.utc)
    access_max_age = max(0, int((secrets.access_expires_at - now).total_seconds()))
    refresh_max_age = max(0, int((secrets.idle_expires_at - now).total_seconds()))
    response.set_cookie(
        "__Host-aigw-access",
        secrets.access_token.reveal(),
        max_age=access_max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        "__Secure-aigw-refresh",
        secrets.refresh_token.reveal(),
        max_age=refresh_max_age,
        path="/api/v1/auth/refresh",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        "aigw-csrf",
        secrets.csrf_token.reveal(),
        max_age=refresh_max_age,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"


def _session_response(
    response: Response,
    principal: Principal,
    secrets: SessionSecrets,
    *,
    requires_password_change: bool,
    requires_mfa_enrollment: bool,
    initial_api_key: CreatedAPIKey | None = None,
) -> SessionAuthentication:
    payload = {
        "member_id": principal.subject_id,
        "system_role": principal.system_role.value,
        "requires_password_change": requires_password_change,
        "requires_mfa_enrollment": requires_mfa_enrollment,
        "access_expires_at": secrets.access_expires_at.isoformat(),
    }
    if initial_api_key is not None:
        payload["initial_api_key"] = _initial_api_key_payload(initial_api_key)
    _apply_session_cookies(response, secrets)
    return SessionAuthentication.model_validate(payload)


def _initial_api_key_payload(created: CreatedAPIKey) -> dict:
    return {
        "id": str(created.key_id),
        "public_id": created.public_id,
        "name": "First device",
        "secret": created.secret.reveal(),
        "scopes": sorted(created.scopes),
        "expires_at": created.expires_at.isoformat() if created.expires_at else None,
    }


@router.post("/login", response_model=SessionAuthentication, response_model_exclude_none=True)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> SessionAuthentication:
    gateway = get_settings().gateway
    client_ip = request_client_ip(request, gateway.trusted_proxy_cidrs)
    throttle_factory = get_session_factory()
    async with throttle_factory.begin() as throttle_session:
        await LoginThrottleService(throttle_session).assert_allowed(payload.username, client_ip)
    identity = _identity_service(session)
    try:
        authenticated = await identity.authenticate_password(
            payload.username,
            payload.password,
        )
        principal = authenticated.principal
        member_id = _member_id(principal)
        member = await session.get(MemberModel, member_id)
        if member is None:
            raise AuthenticationException("Invalid username or password.")
        requires_password_change = bool(member.force_password_change)
        is_super_admin = member.system_role == SystemRole.SUPER_ADMIN.value
        has_active_factor = is_super_admin and await identity.has_active_totp_factor(member_id)
        if has_active_factor and not requires_password_change:
            if payload.totp_code:
                second_factor_valid = await identity.verify_totp_login(member_id, payload.totp_code)
            elif payload.recovery_code:
                second_factor_valid = await identity.consume_recovery_code(
                    member_id,
                    payload.recovery_code,
                    request_id=get_request_id(request),
                )
            else:
                raise MfaCodeRequiredException()
            if not second_factor_valid:
                raise AuthenticationException("Invalid username or password.")
    except AuthenticationException:
        async with throttle_factory.begin() as throttle_session:
            retry_after = await LoginThrottleService(throttle_session).record_failure(payload.username, client_ip)
        if retry_after:
            raise RateLimitException(retry_after)
        raise
    async with throttle_factory.begin() as throttle_session:
        await LoginThrottleService(throttle_session).record_success(payload.username)
    current_time = datetime.now(timezone.utc)
    secrets = await SessionService(session).issue(
        principal,
        now=current_time,
        step_up_at=current_time,
    )
    return _session_response(
        response,
        principal,
        secrets,
        requires_password_change=requires_password_change,
        requires_mfa_enrollment=is_super_admin and not requires_password_change and not has_active_factor,
    )


@router.post("/refresh", response_model=SessionRefresh)
async def refresh(
    payload: EmptyRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> SessionRefresh:
    _verify_origin(request)
    refresh_token = request.cookies.get("__Secure-aigw-refresh")
    if not refresh_token:
        raise AuthenticationException("Invalid session.")
    rotation = await SessionService(session).rotate_refresh(
        refresh_token,
        request_id=get_request_id(request),
        csrf_token=request.headers.get("X-CSRF-Token"),
        require_csrf=True,
    )
    if rotation.status is not RefreshStatus.ROTATED or rotation.session is None:
        raise AuthenticationException("Invalid session.")
    _apply_session_cookies(response, rotation.session)
    return SessionRefresh(
        status="refreshed",
        access_expires_at=rotation.session.access_expires_at,
    )


@router.post("/step-up", response_model=SessionStepUp)
async def step_up(
    payload: StepUpRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> SessionStepUp:
    principal = _principal(request)
    member = await session.get(MemberModel, _member_id(principal))
    if member is None or principal.restricted:
        raise AuthenticationException("Authentication required.")
    identity = _identity_service(session)
    await _verify_current_credentials(
        identity,
        member,
        password=payload.password,
        totp_code=payload.totp_code,
        recovery_code=payload.recovery_code,
        request_id=get_request_id(request),
    )
    credential = await session.get(SessionCredentialModel, _credential_id(principal))
    if credential is None:
        raise AuthenticationException("Authentication required.")
    expires_at = await SessionService(session).record_step_up(
        credential.family_id,
        request_id=get_request_id(request),
    )
    return SessionStepUp(status="reauthenticated", step_up_expires_at=expires_at)


@router.post("/password", response_model=SessionAuthentication, response_model_exclude_none=True)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> SessionAuthentication:
    principal = _principal(request)
    member_id = _member_id(principal)
    current_time = datetime.now(timezone.utc)
    identity = _identity_service(session)
    member = await session.get(MemberModel, member_id)
    if member is None:
        raise AuthenticationException("Authentication required.")
    if not principal.restricted:
        await _verify_current_credentials(
            identity,
            member,
            password=payload.current_password,
            totp_code=payload.current_totp_code,
            recovery_code=payload.recovery_code,
            request_id=get_request_id(request),
        )
    await identity.change_password(
        member_id,
        new_password=payload.password,
        confirmation=payload.confirmation,
        request_id=get_request_id(request),
        now=current_time,
    )
    member = await session.get(MemberModel, member_id)
    if member is None:
        raise AuthenticationException("Authentication required.")
    requires_mfa = (
        member.system_role == SystemRole.SUPER_ADMIN.value
        and not await identity.has_active_totp_factor(member_id)
    )
    next_principal = Principal(
        subject_id=str(member.id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole(member.system_role),
        scopes=frozenset({"*"}),
        restricted=requires_mfa,
    )
    secrets = await SessionService(session).issue(
        next_principal,
        now=current_time,
        step_up_at=current_time,
    )
    initial_api_key = None
    if not requires_mfa:
        initial_api_key = await APIKeyService(
            session,
            configured_api_key_codec(),
        ).create_initial(
            member_id,
            family_id=secrets.family_id,
            request_id=get_request_id(request),
            now=current_time,
        )
    return _session_response(
        response,
        next_principal,
        secrets,
        requires_password_change=False,
        requires_mfa_enrollment=requires_mfa,
        initial_api_key=initial_api_key,
    )


@router.post("/mfa/totp/enroll", response_model=TotpEnrollment)
async def enroll_totp(
    payload: TotpEnrollRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> TotpEnrollment:
    principal = _principal(request)
    member_id = _member_id(principal)
    identity = _identity_service(session)
    member = await session.get(MemberModel, member_id)
    if member is None:
        raise AuthenticationException("Authentication required.")
    if not principal.restricted:
        await _verify_current_credentials(
            identity,
            member,
            password=payload.current_password,
            totp_code=payload.current_totp_code,
            recovery_code=payload.recovery_code,
            request_id=get_request_id(request),
        )
    enrollment = await identity.begin_totp_enrollment(
        member_id,
        request_id=get_request_id(request),
    )
    response.headers["Cache-Control"] = "no-store"
    return TotpEnrollment(
        factor_id=str(enrollment.factor_id),
        secret=enrollment.secret.reveal(),
    )


@router.post("/mfa/totp/confirm", response_model=TotpConfirmation)
async def confirm_totp(
    payload: TotpConfirmRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> TotpConfirmation:
    principal = _principal(request)
    member_id = _member_id(principal)
    current_time = datetime.now(timezone.utc)
    identity = _identity_service(session)
    member = await session.get(MemberModel, member_id)
    if member is None:
        raise AuthenticationException("Authentication required.")
    if not principal.restricted:
        await _verify_current_credentials(
            identity,
            member,
            password=payload.current_password,
            totp_code=payload.current_totp_code,
            recovery_code=payload.recovery_code,
            request_id=get_request_id(request),
        )
    recovery_codes = await identity.confirm_totp_enrollment(
        member_id,
        payload.factor_id,
        payload.code,
        request_id=get_request_id(request),
        now=current_time,
    )
    next_principal = Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=principal.system_role,
        scopes=frozenset({"*"}),
    )
    secrets = await SessionService(session).issue(
        next_principal,
        now=current_time,
        step_up_at=current_time,
    )
    initial_api_key = None
    if principal.restricted:
        initial_api_key = await APIKeyService(
            session,
            configured_api_key_codec(),
        ).create_initial(
            member_id,
            family_id=secrets.family_id,
            request_id=get_request_id(request),
            now=current_time,
        )
    _apply_session_cookies(response, secrets)
    return TotpConfirmation(
        member_id=str(member_id),
        system_role=principal.system_role,
        requires_password_change=False,
        requires_mfa_enrollment=False,
        access_expires_at=secrets.access_expires_at,
        recovery_codes=[value.reveal() for value in recovery_codes],
        initial_api_key=_initial_api_key_payload(initial_api_key) if initial_api_key else None,
    )


@router.post("/logout", response_model=SignOut)
async def logout(
    payload: EmptyRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> SignOut:
    principal = _principal(request)
    if principal.credential_id is None:
        raise AuthenticationException("Authentication required.")
    credential = await session.get(SessionCredentialModel, UUID(principal.credential_id))
    if credential is not None:
        await SessionService(session).revoke_family(
            credential.family_id,
            reason="sign_out",
        )
    response.delete_cookie("__Host-aigw-access", path="/", secure=True, httponly=True, samesite="strict")
    response.delete_cookie("__Secure-aigw-refresh", path="/api/v1/auth/refresh", secure=True, httponly=True, samesite="strict")
    response.delete_cookie("aigw-csrf", path="/", secure=True, httponly=False, samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return SignOut(status="signed_out")
