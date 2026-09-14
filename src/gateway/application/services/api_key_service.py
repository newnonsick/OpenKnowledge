from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services.audit_service import AuditService
from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.exceptions import AuthenticationException, AuthorizationException, RecentAuthenticationRequiredException
from src.gateway.domain.identity import APIKeyStatus, MemberStatus, PermissionProfile, Principal, PrincipalKind, ServiceCredentialSpec, SystemRole
from src.gateway.domain.permission_profiles import profile_scopes
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import APIKeyScopeModel, APIKeySpaceGrantModel, AuditEventModel, MemberModel, PersonalAPIKeyModel, SessionFamilyModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace


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

ALLOWED_SERVICE_PROFILES = frozenset(
    {
        PermissionProfile.READER,
        PermissionProfile.PROJECT_CONTRIBUTOR,
        PermissionProfile.TRUSTED_MAINTAINER,
        PermissionProfile.IMPORT_WORKER,
    }
)

_SERVICE_KEY_NAME_PREFIX = "service:"


@dataclass(frozen=True, slots=True)
class CreatedAPIKey:
    key_id: UUID
    public_id: str
    secret: SecretValue
    scopes: frozenset[str]
    expires_at: datetime | None
    space_grants: frozenset[str] | None = None
    permission_profile: PermissionProfile | None = None


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
        space_grants: set[str] | frozenset[str] | None = None,
        permission_profile: PermissionProfile | str | None = None,
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
        profile = self._normalize_profile(permission_profile)
        if profile is not None:
            allowed_scopes = set(profile_scopes(profile))
            over_broad = requested_scopes - allowed_scopes - {"api_keys:write", "settings:read", "settings:write"}
            if over_broad and profile is not PermissionProfile.HUMAN_ADMIN:
                raise ValueError("API key scopes exceed the permission profile")
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
        normalized_grants = await self._validate_space_grants(member_id, space_grants)
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
        if normalized_grants is not None:
            self._session.add_all(
                APIKeySpaceGrantModel(api_key_id=model.id, space_id=space_id)
                for space_id in sorted(normalized_grants)
            )
        self._audit.record(
            actor_member_id=member_id,
            actor_kind="member",
            request_id=request_id,
            action="api_key.created",
            resource_type="api_key",
            resource_id=str(model.id),
            details={
                "name": normalized_name,
                "scopes": sorted(requested_scopes),
                "space_grants": sorted(normalized_grants) if normalized_grants is not None else None,
                "permission_profile": profile.value if profile is not None else None,
            },
        )
        await self._session.flush()
        return CreatedAPIKey(
            model.id,
            issued.public_id,
            issued.secret,
            requested_scopes,
            expires_at,
            normalized_grants,
            profile,
        )

    async def create_service_key(
        self,
        actor: Principal,
        *,
        application_id: str,
        credential_name: str,
        spec: ServiceCredentialSpec,
        request_id: str,
        now: datetime | None = None,
    ) -> CreatedAPIKey:
        normalized_application = application_id.strip()
        if not normalized_application or len(normalized_application) > 120:
            raise ValueError("Service application id must contain between 1 and 120 characters")
        normalized_name = credential_name.strip()
        if not normalized_name or len(normalized_name) > 120:
            raise ValueError("API key name must contain between 1 and 120 characters")
        if not actor.active:
            raise AuthorizationException()
        if spec.permission_profile not in ALLOWED_SERVICE_PROFILES:
            raise ValueError("Service credentials must not use the human admin profile")
        if not is_allowed(AuthorizationContext(actor), Action.API_KEY_MANAGE):
            raise AuthorizationException()
        if actor.kind is PrincipalKind.SERVICE:
            raise AuthorizationException()
        current_time = now or datetime.now(timezone.utc)
        if spec.expires_at is not None and spec.expires_at <= current_time:
            raise ValueError("API key expiration must be in the future")
        owner_id = UUID(actor.subject_id)
        member = await self._session.get(MemberModel, owner_id)
        if member is None or member.status != MemberStatus.ACTIVE.value:
            raise AuthorizationException()
        normalized_grants = await self._validate_service_grants(owner_id, spec.space_grants)
        if actor.kind is PrincipalKind.API_KEY and actor.space_grants is not None:
            outside = normalized_grants - set(actor.space_grants)
            if outside:
                raise AuthorizationException()
        requested_scopes = frozenset(profile_scopes(spec.permission_profile))
        issued = self._codec.issue()
        model = PersonalAPIKeyModel(
            id=uuid4(),
            member_id=owner_id,
            public_id=issued.public_id,
            key_digest=issued.digest,
            pepper_version=issued.pepper_version,
            name=f"{_SERVICE_KEY_NAME_PREFIX}{normalized_application}:{normalized_name}"[:120],
            status=APIKeyStatus.ACTIVE.value,
            expires_at=spec.expires_at,
            is_service=True,
            service_profile=spec.permission_profile.value,
        )
        self._session.add(model)
        await self._session.flush()
        self._session.add_all(
            APIKeyScopeModel(api_key_id=model.id, scope=scope)
            for scope in sorted(requested_scopes)
        )
        self._session.add_all(
            APIKeySpaceGrantModel(api_key_id=model.id, space_id=space_id)
            for space_id in sorted(normalized_grants)
        )
        self._audit.record(
            actor_member_id=owner_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="service_key.created",
            resource_type="api_key",
            resource_id=str(model.id),
            details={
                "application_id": normalized_application,
                "key_name": normalized_name,
                "granted_by": actor.credential_id,
                "grant_owner": str(owner_id),
                "grants": sorted(normalized_grants),
                "permission_profile": spec.permission_profile.value,
                "service_kind": PrincipalKind.SERVICE.value,
            },
        )
        await self._session.flush()
        return CreatedAPIKey(
            model.id,
            issued.public_id,
            issued.secret,
            requested_scopes,
            spec.expires_at,
            normalized_grants,
            spec.permission_profile,
        )

    async def _validate_service_grants(
        self,
        owner_id: UUID,
        space_grants: frozenset[str],
    ) -> frozenset[str]:
        normalized = frozenset(str(space_id).strip() for space_id in space_grants if str(space_id).strip())
        if not normalized:
            raise ValueError("Service credentials require at least one space grant")
        if len(normalized) > 100:
            raise ValueError("Too many space grants")
        rows = await self._session.scalars(
            select(Workspace.id).where(Workspace.id.in_(normalized))
        )
        known = frozenset(rows)
        unknown = normalized - known
        if unknown:
            raise ValueError("Unknown space in grants")
        owned = frozenset(
            await self._session.scalars(
                select(SpaceMembershipModel.space_id).where(
                    SpaceMembershipModel.member_id == owner_id,
                    SpaceMembershipModel.space_id.in_(normalized),
                )
            )
        )
        outside = normalized - set(owned)
        if outside:
            raise ValueError("Cannot grant spaces the member cannot access")
        return normalized

    async def _service_marker(self, key_id: UUID) -> tuple[bool, PermissionProfile | None]:
        row = await self._session.scalar(
            select(AuditEventModel.details).where(
                AuditEventModel.resource_type == "api_key",
                AuditEventModel.resource_id == str(key_id),
                AuditEventModel.action == "service_key.created",
            ).order_by(AuditEventModel.occurred_at.desc()).limit(1)
        )
        if not row:
            return False, None
        details = row if isinstance(row, dict) else {}
        if details.get("service_kind") != PrincipalKind.SERVICE.value:
            return False, None
        raw_profile = details.get("permission_profile")
        if raw_profile is None:
            return True, None
        try:
            profile = PermissionProfile(str(raw_profile))
        except ValueError:
            return True, None
        if profile not in ALLOWED_SERVICE_PROFILES:
            return True, None
        return True, profile

    @staticmethod
    def _normalize_profile(value: PermissionProfile | str | None) -> PermissionProfile | None:
        if value is None:
            return None
        if isinstance(value, PermissionProfile):
            return value
        try:
            return PermissionProfile(str(value))
        except ValueError as exc:
            raise ValueError("Unknown permission profile") from exc

    async def _validate_space_grants(
        self,
        member_id: UUID,
        space_grants: set[str] | frozenset[str] | None,
    ) -> frozenset[str] | None:
        if space_grants is None:
            return None
        normalized = frozenset(str(space_id).strip() for space_id in space_grants if str(space_id).strip())
        if not normalized:
            raise ValueError("Space grants must not be empty when provided")
        if len(normalized) > 100:
            raise ValueError("Too many space grants")
        rows = await self._session.scalars(
            select(Workspace.id).where(Workspace.id.in_(normalized))
        )
        known = frozenset(rows)
        unknown = normalized - known
        if unknown:
            raise ValueError("Unknown space in grants")
        owned = frozenset(
            await self._session.scalars(
                select(SpaceMembershipModel.space_id).where(
                    SpaceMembershipModel.member_id == member_id,
                    SpaceMembershipModel.space_id.in_(normalized),
                )
            )
        )
        outside = normalized - set(owned)
        if outside:
            raise ValueError("Cannot grant spaces the member cannot access")
        return normalized

    async def key_space_grants(self, key_id: UUID) -> frozenset[str] | None:
        rows = await self._session.scalars(
            select(APIKeySpaceGrantModel.space_id).where(APIKeySpaceGrantModel.api_key_id == key_id)
        )
        collected = frozenset(rows)
        has_rows = await self._session.scalar(
            select(APIKeySpaceGrantModel.api_key_id).where(APIKeySpaceGrantModel.api_key_id == key_id).limit(1)
        )
        if has_rows is None:
            return None
        return collected

    async def resolve(self, raw_key: str, *, now: datetime | None = None) -> Principal:
        current_time = now or datetime.now(timezone.utc)
        try:
            parsed = self._codec.parse(raw_key)
        except ValueError as exc:
            raise AuthenticationException("Invalid API key provided.") from exc
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
            raise AuthenticationException("Invalid API key provided.")
        member = await self._session.get(MemberModel, key.member_id)
        if member is None or member.status != MemberStatus.ACTIVE.value:
            raise AuthenticationException("Invalid API key provided.")
        scopes = frozenset(
            await self._session.scalars(
                select(APIKeyScopeModel.scope).where(APIKeyScopeModel.api_key_id == key.id)
            )
        )
        grants = await self.key_space_grants(key.id)
        if grants is not None:
            live = frozenset(
                await self._session.scalars(
                    select(SpaceMembershipModel.space_id).where(
                        SpaceMembershipModel.member_id == member.id,
                        SpaceMembershipModel.space_id.in_(grants),
                    )
                )
            )
            grants = live
        is_service, profile = await self._service_marker(key.id)
        if not is_service and bool(getattr(key, "is_service", False)):
            is_service = True
            profile = self._service_column_profile(key)
        key.last_used_at = current_time
        if is_service:
            return Principal(
                subject_id=str(member.id),
                kind=PrincipalKind.SERVICE,
                system_role=SystemRole(member.system_role),
                scopes=scopes,
                credential_id=str(key.id),
                space_grants=grants,
                permission_profile=profile,
            )
        key_profile = await self._legacy_key_profile(key.id)
        return Principal(
            subject_id=str(member.id),
            kind=PrincipalKind.API_KEY,
            system_role=SystemRole(member.system_role),
            scopes=scopes,
            credential_id=str(key.id),
            space_grants=grants,
            permission_profile=key_profile,
        )

    @staticmethod
    def _service_column_profile(key: PersonalAPIKeyModel) -> PermissionProfile | None:
        raw_profile = getattr(key, "service_profile", None)
        if raw_profile is None:
            return None
        try:
            profile = PermissionProfile(str(raw_profile))
        except ValueError:
            return None
        if profile not in ALLOWED_SERVICE_PROFILES:
            return None
        return profile

    async def _legacy_key_profile(self, key_id: UUID) -> PermissionProfile | None:
        row = await self._session.scalar(
            select(AuditEventModel.details).where(
                AuditEventModel.resource_type == "api_key",
                AuditEventModel.resource_id == str(key_id),
                AuditEventModel.action == "api_key.created",
            ).order_by(AuditEventModel.occurred_at.desc()).limit(1)
        )
        if not row or not isinstance(row, dict):
            return None
        raw_profile = row.get("permission_profile")
        if raw_profile is None:
            return None
        try:
            profile = PermissionProfile(str(raw_profile))
        except ValueError:
            return None
        return profile

    async def owned_key(self, actor: Principal, key_id: UUID) -> PersonalAPIKeyModel:
        try:
            actor_id = UUID(actor.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc
        key = await self._session.get(PersonalAPIKeyModel, key_id)
        if key is None:
            raise AuthorizationException()
        if key.member_id != actor_id and actor.system_role is not SystemRole.SUPER_ADMIN:
            raise AuthorizationException()
        if not actor.active:
            raise AuthorizationException()
        return key

    async def key_identity(self, key: PersonalAPIKeyModel) -> tuple[UUID, PrincipalKind]:
        is_service, _ = await self._service_marker(key.id)
        kind = PrincipalKind.SERVICE if is_service else PrincipalKind.API_KEY
        return key.member_id, kind

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
            raise AuthorizationException()
        owns_key = actor.subject_id == str(key.member_id)
        may_manage = is_allowed(AuthorizationContext(actor), Action.API_KEY_MANAGE)
        if not actor.active or (not owns_key and actor.system_role is not SystemRole.SUPER_ADMIN):
            raise AuthorizationException()
        if not owns_key and not may_manage:
            raise AuthorizationException()
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
