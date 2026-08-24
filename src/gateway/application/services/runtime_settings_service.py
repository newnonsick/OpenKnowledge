from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.audit_service import AuditService
from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.exceptions import AuthorizationException, ConcurrencyConflictException, RecentAuthenticationRequiredException, ResourceConflictException
from src.gateway.domain.identity import Principal, PrincipalKind
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.identity_models import SessionFamilyModel
from src.gateway.infrastructure.persistence.runtime_settings_models import RuntimeSettingRevisionModel


class RetrievalRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=10, ge=1, le=100)
    branch_limit: int = Field(default=40, ge=1, le=500)
    lexical_weight: float = Field(default=1.0, ge=0, le=100)
    vector_weight: float = Field(default=1.0, ge=0, le=100)
    rrf_k: int = Field(default=60, ge=1, le=1000)
    minimum_lexical_score: float = Field(default=0.01, ge=0, le=1)
    minimum_vector_similarity: float = Field(default=0.55, ge=0, le=1)
    max_hits_per_source: int = Field(default=2, ge=1, le=20)
    active_space_boost: float = Field(default=0.08, ge=0, le=1)
    semantic_policy: Literal["prefer", "required", "disabled"] = "prefer"
    hnsw_ef_search: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_branches(self):
        if self.lexical_weight == 0 and self.vector_weight == 0:
            raise ValueError("At least one retrieval branch must be enabled")
        if self.semantic_policy == "required" and self.vector_weight == 0:
            raise ValueError("Required semantic retrieval needs a vector weight")
        return self


class ChunkingRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: int = Field(default=2000, ge=200, le=10000)
    overlap: int = Field(default=200, ge=0, le=2000)
    max_chunks: int = Field(default=10000, ge=1, le=50000)

    @model_validator(mode="after")
    def validate_overlap(self):
        if self.overlap >= self.size:
            raise ValueError("Chunk overlap must be smaller than chunk size")
        return self


class ToolRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_tools_enabled: bool = True
    mutation_tools_enabled: bool = True
    destructive_tools_require_confirmation: bool = True


class FeatureRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic_retrieval_enabled: bool = True
    retrieval_explanations_enabled: bool = True


class RuntimeSettingsValues(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retrieval: RetrievalRuntimeSettings = Field(default_factory=RetrievalRuntimeSettings)
    chunking: ChunkingRuntimeSettings = Field(default_factory=ChunkingRuntimeSettings)
    tools: ToolRuntimeSettings = Field(default_factory=ToolRuntimeSettings)
    features: FeatureRuntimeSettings = Field(default_factory=FeatureRuntimeSettings)

    @model_validator(mode="after")
    def validate_dependencies(self):
        if self.retrieval.semantic_policy == "required" and not self.features.semantic_retrieval_enabled:
            raise ValueError("Required semantic retrieval cannot be disabled by a feature flag")
        return self


@dataclass(frozen=True, slots=True)
class RuntimeSettingsRevision:
    id: UUID | None
    revision: int
    base_revision: int
    state: str
    values: RuntimeSettingsValues
    created_at: datetime | None
    activated_at: datetime | None


class RuntimeSettingsService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        step_up_window: timedelta = timedelta(minutes=10),
        dependency_probe: Callable[[RuntimeSettingsValues], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._step_up_window = step_up_window
        self._dependency_probe = dependency_probe
        self._audit = AuditService(AuditRepository(session))

    async def active(self, *, for_update: bool = False) -> RuntimeSettingsRevision:
        query = select(RuntimeSettingRevisionModel).where(
            RuntimeSettingRevisionModel.state == "active"
        )
        if for_update:
            query = query.with_for_update()
        model = await self._session.scalar(query)
        if model is None:
            return RuntimeSettingsRevision(
                None,
                0,
                0,
                "active",
                RuntimeSettingsValues(),
                None,
                None,
            )
        return self._to_domain(model)

    async def history(
        self,
        *,
        limit: int = 50,
        before_revision: int | None = None,
        state: str | None = None,
    ) -> list[RuntimeSettingsRevision]:
        if limit < 1 or limit > 101:
            raise ValueError("Runtime settings history limit must be between 1 and 101")
        query = (
            select(RuntimeSettingRevisionModel)
            .where(RuntimeSettingRevisionModel.state.in_(("active", "superseded")))
            .order_by(RuntimeSettingRevisionModel.revision.desc())
            .limit(limit)
        )
        if before_revision is not None:
            query = query.where(RuntimeSettingRevisionModel.revision < before_revision)
        if state is not None:
            query = query.where(RuntimeSettingRevisionModel.state == state)
        models = list(
            await self._session.scalars(
                query
            )
        )
        return [self._to_domain(model) for model in models]

    async def create_draft(
        self,
        actor: Principal,
        *,
        family_id: UUID,
        base_revision: int,
        values: RuntimeSettingsValues,
        reason: str,
        request_id: str,
        now: datetime | None = None,
    ) -> RuntimeSettingsRevision:
        current_time = now or datetime.now(timezone.utc)
        actor_id = await self._require_recent_admin(actor, family_id, current_time)
        clean_reason = self._reason(reason)
        active = await self.active(for_update=True)
        if base_revision != active.revision:
            raise ConcurrencyConflictException(
                "runtime_settings",
                base_revision,
                active.revision,
            )
        if self._session.bind and self._session.bind.dialect.name == "postgresql":
            await self._session.execute(text("SELECT pg_advisory_xact_lock(7046029254386353132)"))
        maximum = int(
            await self._session.scalar(select(func.max(RuntimeSettingRevisionModel.revision)))
            or 0
        )
        model = RuntimeSettingRevisionModel(
            id=uuid4(),
            revision=maximum + 1,
            base_revision=base_revision,
            state="draft",
            values=values.model_dump(mode="json"),
            draft_reason=clean_reason,
            created_by_member_id=actor_id,
            created_at=current_time,
        )
        self._session.add(model)
        self._audit.record(
            actor_member_id=actor_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="runtime_settings.draft_created",
            resource_type="runtime_settings",
            resource_id=str(model.id),
            details={"base_revision": base_revision, "candidate_revision": model.revision},
        )
        await self._session.flush()
        return self._to_domain(model)

    async def activate(
        self,
        actor: Principal,
        *,
        family_id: UUID,
        draft_id: UUID,
        expected_active_revision: int,
        reason: str,
        request_id: str,
        now: datetime | None = None,
    ) -> RuntimeSettingsRevision:
        current_time = now or datetime.now(timezone.utc)
        actor_id = await self._require_recent_admin(actor, family_id, current_time)
        clean_reason = self._reason(reason)
        active = await self.active(for_update=True)
        if expected_active_revision != active.revision:
            raise ConcurrencyConflictException(
                "runtime_settings",
                expected_active_revision,
                active.revision,
            )
        draft = await self._session.scalar(
            select(RuntimeSettingRevisionModel)
            .where(RuntimeSettingRevisionModel.id == draft_id)
            .with_for_update()
        )
        if draft is None or draft.state != "draft":
            raise ResourceConflictException("Runtime settings draft is unavailable.")
        if draft.base_revision != active.revision:
            raise ConcurrencyConflictException(
                "runtime_settings",
                draft.base_revision,
                active.revision,
            )
        values = RuntimeSettingsValues.model_validate(draft.values)
        await self._preflight(values)
        if active.id is not None:
            current = await self._session.get(RuntimeSettingRevisionModel, active.id)
            if current is None:
                raise ResourceConflictException()
            current.state = "superseded"
            await self._session.flush()
        draft.state = "active"
        draft.activation_reason = clean_reason
        draft.activated_by_member_id = actor_id
        draft.activated_at = current_time
        self._audit.record(
            actor_member_id=actor_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="runtime_settings.activated",
            resource_type="runtime_settings",
            resource_id=str(draft.id),
            details={"old_revision": active.revision, "new_revision": draft.revision},
        )
        await self._session.flush()
        return self._to_domain(draft)

    async def rollback(
        self,
        actor: Principal,
        *,
        family_id: UUID,
        target_revision: int,
        expected_active_revision: int,
        reason: str,
        request_id: str,
        now: datetime | None = None,
    ) -> RuntimeSettingsRevision:
        current_time = now or datetime.now(timezone.utc)
        actor_id = await self._require_recent_admin(actor, family_id, current_time)
        clean_reason = self._reason(reason)
        active = await self.active(for_update=True)
        if expected_active_revision != active.revision:
            raise ConcurrencyConflictException(
                "runtime_settings",
                expected_active_revision,
                active.revision,
            )
        target = await self._session.scalar(
            select(RuntimeSettingRevisionModel)
            .where(
                RuntimeSettingRevisionModel.revision == target_revision,
                RuntimeSettingRevisionModel.state == "superseded",
            )
            .with_for_update()
        )
        if target is None:
            raise ResourceConflictException("The requested runtime settings revision cannot be restored.")
        values = RuntimeSettingsValues.model_validate(target.values)
        await self._preflight(values)
        if self._session.bind and self._session.bind.dialect.name == "postgresql":
            await self._session.execute(text("SELECT pg_advisory_xact_lock(7046029254386353132)"))
        maximum = int(
            await self._session.scalar(select(func.max(RuntimeSettingRevisionModel.revision)))
            or 0
        )
        current = await self._session.get(RuntimeSettingRevisionModel, active.id)
        if current is None:
            raise ResourceConflictException()
        current.state = "superseded"
        await self._session.flush()
        restored = RuntimeSettingRevisionModel(
            id=uuid4(),
            revision=maximum + 1,
            base_revision=active.revision,
            state="active",
            values=values.model_dump(mode="json"),
            draft_reason=clean_reason,
            activation_reason=clean_reason,
            created_by_member_id=actor_id,
            activated_by_member_id=actor_id,
            created_at=current_time,
            activated_at=current_time,
        )
        self._session.add(restored)
        self._audit.record(
            actor_member_id=actor_id,
            actor_kind=actor.kind.value,
            request_id=request_id,
            action="runtime_settings.rolled_back",
            resource_type="runtime_settings",
            resource_id=str(restored.id),
            details={
                "old_revision": active.revision,
                "new_revision": restored.revision,
                "target_revision": target_revision,
            },
        )
        await self._session.flush()
        return self._to_domain(restored)

    async def _preflight(self, values: RuntimeSettingsValues) -> None:
        if self._dependency_probe is None:
            return
        try:
            await self._dependency_probe(values)
        except Exception as exc:
            raise ResourceConflictException("Runtime settings dependency preflight failed.") from exc

    async def _require_recent_admin(
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
            raise RecentAuthenticationRequiredException()
        return actor_id

    @staticmethod
    def _reason(value: str) -> str:
        clean = value.strip()
        if len(clean) < 5 or len(clean) > 500:
            raise ValueError("A reason between 5 and 500 characters is required")
        return clean

    @staticmethod
    def _to_domain(model: RuntimeSettingRevisionModel) -> RuntimeSettingsRevision:
        return RuntimeSettingsRevision(
            model.id,
            model.revision,
            model.base_revision,
            model.state,
            RuntimeSettingsValues.model_validate(model.values),
            model.created_at,
            model.activated_at,
        )
