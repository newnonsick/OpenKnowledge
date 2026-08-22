from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services.audit_service import AuditService
from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.exceptions import AuthenticationException, RecentAuthenticationRequiredException
from src.gateway.domain.identity import APIKeyStatus, MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import APIKeyScopeModel, MemberModel, PersonalAPIKeyModel, SessionFamilyModel


ALLOWED_API_KEY_SCOPES = frozenset(
    {
        "api_keys:write",
        "chat:write",
        "knowledge:read",
        "knowledge:write",
        "spaces:members",
        "spaces:read",
        "spaces:write",
        "settings:read",
        "settings:write",
    }
)

INITIAL_API_KEY_SCOPES = frozenset(
    {"chat:write", "knowledge:read", "knowledge:write", "spaces:read"}
)


@dataclass(frozen=True, slots=True)
class CreatedAPIKey:
    key_id: UUID
    public_id: str
    secret: SecretValue
    scopes: frozenset[str]
    expires_at: datetime | None


class APIKeyService:
    def __init__(
        self,
        session: AsyncSession,
        codec: APIKeyCodec,
        *,
        step_up_window: timedelta = timedelta(minutes=10),
    ) -> None:
        self._session = session
        self._codec = codec
        self._step_up_window = step_up_window
        self._audit = AuditService(AuditRepository(session))

    async def create_initial(
        self,
        member_id: UUID,
        *,
        family_id: UUID,
        request_id: str,
        now: datetime | None = None,
    ) -> CreatedAPIKey | None:
        existing = await self._session.scalar(
            select(PersonalAPIKeyModel.id)
            .where(PersonalAPIKeyModel.member_id == member_id)
            .limit(1)
        )
        if existing is not None:
            return None
        return await self.create(
            member_id,
            family_id=family_id,
            name="First device",
            scopes=INITIAL_API_KEY_SCOPES,
            request_id=request_id,
            now=now,
        )

    async def create(
        self,
        member_id: UUID,
        *,
        family_id: UUID,
        name: str,
        scopes: set[str] | frozenset[str],
        request_id: str,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> CreatedAPIKey:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 120:
            raise ValueError("API key name must contain between 1 and 120 characters")
        requested_scopes = frozenset(scopes)
        unsupported = requested_scopes - ALLOWED_API_KEY_SCOPES
        if unsupported:
            raise ValueError("Unsupported API key scope")
        if not requested_scopes:
            raise ValueError("At least one API key scope is required")
        current_time = now or datetime.now(timezone.utc)
        member = await self._session.get(MemberModel, member_id)
        family = await self._session.scalar(
            select(SessionFamilyModel)
            .where(SessionFamilyModel.id == family_id)
            .with_for_update()
        )
        if (
            member is None
            or member.status != MemberStatus.ACTIVE.value
            or family is None
            or family.member_id != member_id
            or family.revoked_at is not None
            or current_time >= family.idle_expires_at
            or current_time >= family.absolute_expires_at
            or family.last_step_up_at is None
            or current_time - family.last_step_up_at > self._step_up_window
        ):
            raise RecentAuthenticationRequiredException()
        if expires_at is not None and expires_at <= current_time:
            raise ValueError("API key expiration must be in the future")
        issued = self._codec.issue()
        model = PersonalAPIKeyModel(
            id=uuid4(),
            member_id=member_id,
            public_id=issued.public_id,
            key_digest=issued.digest,
            pepper_version=issued.pepper_version,
            name=normalized_name,
            status=APIKeyStatus.ACTIVE.value,
            expires_at=expires_at,
        )
        self._session.add(model)
        await self._session.flush()
        self._session.add_all(
            APIKeyScopeModel(api_key_id=model.id, scope=scope)
            for scope in sorted(requested_scopes)
        )
        self._audit.record(
            actor_member_id=member_id,
            actor_kind="member",
            request_id=request_id,
            action="api_key.created",
            resource_type="api_key",
            resource_id=str(model.id),
            details={"name": normalized_name, "scopes": sorted(requested_scopes)},
        )
        await self._session.flush()
        return CreatedAPIKey(
            model.id,
            issued.public_id,
            issued.secret,
            requested_scopes,
            expires_at,
        )

    async def resolve(self, raw_key: str, *, now: datetime | None = None) -> Principal:
        current_time = now or datetime.now(timezone.utc)
        try:
            parsed = self._codec.parse(raw_key)
        except ValueError as exc:
            raise AuthenticationException("Invalid API key.") from exc
        key = await self._session.scalar(
            select(PersonalAPIKeyModel).where(PersonalAPIKeyModel.public_id == parsed.public_id)
        )
        if (
            key is None
            or key.status != APIKeyStatus.ACTIVE.value
            or key.revoked_at is not None
            or (key.expires_at is not None and current_time >= key.expires_at)
            or parsed.pepper_version != key.pepper_version
            or not self._codec.verify(raw_key, key.key_digest, pepper_version=key.pepper_version)
        ):
            raise AuthenticationException("Invalid API key.")
        member = await self._session.get(MemberModel, key.member_id)
        if member is None or member.status != MemberStatus.ACTIVE.value:
            raise AuthenticationException("Invalid API key.")
        scopes = frozenset(
            await self._session.scalars(
                select(APIKeyScopeModel.scope).where(APIKeyScopeModel.api_key_id == key.id)
            )
        )
        key.last_used_at = current_time
        return Principal(
            subject_id=str(member.id),
            kind=PrincipalKind.API_KEY,
            system_role=SystemRole(member.system_role),
            scopes=scopes,
            credential_id=str(key.id),
        )

    async def revoke(
        self,
        actor: Principal,
        key_id: UUID,
        *,
        request_id: str,
        now: datetime | None = None,
    ) -> None:
        key = await self._session.scalar(
            select(PersonalAPIKeyModel)
            .where(PersonalAPIKeyModel.id == key_id)
            .with_for_update()
        )
        if key is None:
            raise AuthenticationException("API key is unavailable.")
        owns_key = actor.subject_id == str(key.member_id)
        may_manage = is_allowed(AuthorizationContext(actor), Action.API_KEY_MANAGE)
        if not actor.active or (not owns_key and actor.system_role is not SystemRole.SUPER_ADMIN) or not may_manage:
            raise AuthenticationException("API key is unavailable.")
        current_time = now or datetime.now(timezone.utc)
        key.status = APIKeyStatus.REVOKED.value
        key.revoked_at = key.revoked_at or current_time
        self._audit.record(
            actor_member_id=UUID(actor.subject_id),
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="api_key.revoked",
            resource_type="api_key",
            resource_id=str(key.id),
        )
        await self._session.flush()
