from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.services.action_review_service import review_pending_action
from src.gateway.application.services.ai_management_service import AIManagementService
from src.gateway.application.services.audit_service import AuditService
from src.gateway.application.services.authorized_retrieval_service import AuthorizedRetrievalService
from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.services.chunk_policy_service import (
    chunk_policy_payload,
    effective_chunk_policy,
    validate_chunk_policy,
)
from src.gateway.application.services.document_upload_service import DocumentUploadService
from src.gateway.application.services.embedding_generation_service import EmbeddingGenerationService
from src.gateway.application.services.embedding_lifecycle_service import (
    EmbeddingGenerationLifecycleService,
)
from src.gateway.application.services.embedding_reembed_service import EmbeddingReembedService, REEMBED_TARGETS
from src.gateway.application.services.context_assembly_service import (
    AssembleContextQuery,
    ContextAssembler,
    context_package_response,
)
from src.gateway.application.services.evidence_service import EvidenceReference, EvidenceService, evidence_response
from src.gateway.application.services.git_connector_service import (
    GitConnectorService,
    connector_payload,
    sync_result_payload,
)
from src.gateway.application.services.idempotency_service import IdempotencyService, ReservationStatus
from src.gateway.application.services.ingestion_job_service import IngestionJobService
from src.gateway.application.services.knowledge_management_service import KnowledgeManagementService
from src.gateway.application.services.member_administration_service import MemberAdministrationService
from src.gateway.application.services.runtime_settings_service import EffectiveRuntimePolicy, RuntimeSettingsService, RuntimeSettingsRevision, RuntimeSettingsValues
from src.gateway.application.services.session_service import SessionService
from src.gateway.application.services.space_service import SpaceService
from src.gateway.application.use_cases import (
    CreateKnowledgeCommand,
    DeleteKnowledgeCommand,
    KnowledgeCommands,
    RetrievalQueries,
    SearchKnowledgeQuery,
    TransitionKnowledgeCommand,
    UpdateKnowledgeCommand,
    UseCaseContext,
    redact_retrieval_payload,
    retrieval_payload,
)
from src.gateway.application.services.permission_service import require_profile_route, require_profile_tool
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import AuthenticationException, AuthorizationException, ResourceConflictException, ValidationException
from src.gateway.domain.authorization import Action, narrow_requested_spaces, resolve_request_space_scope
from src.gateway.domain.identity import MemberStatus, PermissionProfile, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.domain.entities import KnowledgeItem as DomainKnowledgeItem
from src.gateway.infrastructure.database import get_db_session, get_session_factory
from src.gateway.presentation.api_keys import configured_api_key_codec
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionChunkModel, DocumentRevisionModel, EmbeddingGenerationModel, IngestionJobModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.models import KnowledgeItem as KnowledgeItemModel, KnowledgeRevision as KnowledgeRevisionModel
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from src.gateway.infrastructure.persistence.identity_models import APIKeyScopeModel, APIKeySpaceGrantModel, AuditEventModel, MemberModel, MFAFactorModel, PendingAIActionModel, PersonalAPIKeyModel, SessionCredentialModel, SessionFamilyModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.infrastructure.persistence.runtime_settings_models import RuntimeSettingRevisionModel
from src.gateway.infrastructure.runtime_settings_provider import load_active_retrieval_settings, load_active_runtime_policy
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from src.gateway.presentation.authorization import require_principal, require_scope
from src.gateway.presentation.request_context import get_request_id
from src.gateway.application.services.quota_service import quota_service_from_settings
from src.gateway.presentation.schemas.management_responses import (
    MANAGEMENT_ERROR_RESPONSES,
    AdminSpaceSummary,
    APIKeySummary,
    AIToolExecution,
    AIToolList,
    AuditEvent,
    ClientCapabilities,
    ConfirmedAIAction,
    ContextPackageDetail,
    CreatedAPIKey,
    CreatedMember,
    CreatedSpace,
    CredentialQuotaUsage,
    CurrentMember,
    EmbeddingGenerationSummary,
    EvidenceDetail,
    GenerationTransition,
    IngestionJob,
    IngestionMutation,
    KnowledgeDetail,
    KnowledgeEnrichmentReceipt,
    KnowledgeExport,
    KnowledgeImportSummary,
    KnowledgeSummary,
    KnowledgeTransition,
    MemberSummary,
    OperationSummary,
    Page,
    PendingAIAction,
    PendingAIActionDetail,
    ReindexEnqueueResult,
    ReindexStatus,
    ReindexTargetStatus,
    ResetMemberPassword,
    RetrievalResult,
    ReviewedKnowledge,
    RuntimeSettings,
    SessionSummary,
    SourceConnectorSummary,
    SourceConnectorSyncReceipt,
    SourceSummary,
    SourceUploadReceipt,
    SpaceDetail,
    SpaceMember,
    SpaceMemberCandidate,
    SpaceMembership,
    SpaceSummary,
    StaleKnowledgeItem,
    WebhookEventDetail,
)
from src.gateway.observability import increment_metric


def default_retrieval_embedding_client():
    return HTTPEmbeddingClient()


def _retrieval_service() -> AuthorizedRetrievalService:
    return AuthorizedRetrievalService(
        PostgresRetrievalUnitRepository(get_session_factory()),
        default_retrieval_embedding_client(),
        runtime_settings_provider=load_active_retrieval_settings,
    )


router = APIRouter(prefix="/api/v1", tags=["Management"], responses=MANAGEMENT_ERROR_RESPONSES)
KnowledgeTag = Annotated[str, Field(min_length=1, max_length=80)]


@router.get("/operations/summary", response_model=OperationSummary)
async def operations_summary(
    principal: Principal = Depends(require_scope("settings:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "settings.inspect")
    space_ids = await AuthorizationService(session).effective_space_ids(_actor_id(principal), principal=principal)
    states = ("queued", "retry_wait", "running", "cancellation_requested", "succeeded", "failed", "cancelled")
    if space_ids:
        job_rows = await session.execute(
            select(IngestionJobModel.state, func.count(IngestionJobModel.id))
            .where(IngestionJobModel.space_id.in_(space_ids))
            .group_by(IngestionJobModel.state)
        )
        job_counts = {state: int(count) for state, count in job_rows}
        referenced_bytes = int(
            await session.scalar(
                select(func.coalesce(func.sum(DocumentRevisionModel.size_bytes), 0)).where(
                    DocumentRevisionModel.space_id.in_(space_ids),
                    DocumentRevisionModel.storage_key.is_not(None),
                )
            )
            or 0
        )
    else:
        job_counts = {}
        referenced_bytes = 0
    ingestion = {state: job_counts.get(state, 0) for state in states}
    generation_active = await session.scalar(
        select(EmbeddingGenerationModel.id).where(
            EmbeddingGenerationModel.purpose == "retrieval",
            EmbeddingGenerationModel.status == "active",
        )
    )
    active_settings = await RuntimeSettingsService(session).active()
    stale_after_days = get_settings().gateway.stale_after_days
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_after_days)
    stale_items = 0
    if space_ids:
        stale_items = int(
            await session.scalar(
                select(func.count(KnowledgeItemModel.id)).where(
                    KnowledgeItemModel.is_deleted.is_(False),
                    KnowledgeItemModel.archived_at.is_(None),
                    KnowledgeItemModel.workspace_id.in_(space_ids),
                    KnowledgeItemModel.updated_at < stale_cutoff,
                )
            )
            or 0
        )
    return {
        "scope": "accessible_spaces",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "spaces": len(space_ids),
        "ingestion": ingestion,
        "storage": {"referenced_bytes": referenced_bytes},
        "retrieval": {"embedding_generation_active": generation_active is not None},
        "settings_revision": active_settings.revision,
        "stale_knowledge": {"stale_after_days": stale_after_days, "stale_items": stale_items},
    }


class SpaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


class MembershipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["editor", "reader"]


class ChunkPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_size: int = Field(ge=64, le=32000)
    chunk_overlap: int = Field(ge=0)
    chunk_strategy: Literal["fixed", "semantic"]


class OwnershipTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_member_id: UUID
    expected_revision: int = Field(ge=1)


class EmergencyOwnershipTransferRequest(OwnershipTransferRequest):
    reason: str = Field(min_length=5, max_length=500)


class APIKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1, max_length=16)
    expires_at: datetime | None = None
    space_grants: list[str] | None = Field(default=None, max_length=100)
    permission_profile: Literal["reader", "project_contributor", "trusted_maintainer", "import_worker", "human_admin"] | None = None


class MemberCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)


class MemberUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=255)
    status: MemberStatus
    system_role: SystemRole


class KnowledgeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=1_000_000)
    tags: list[KnowledgeTag] = Field(default_factory=list, max_length=32)
    lifecycle_status: Literal["observation", "candidate", "accepted"] = "accepted"
    origin: str | None = Field(default=None, min_length=1, max_length=200)
    source_detail: str | None = Field(default=None, min_length=1, max_length=2000)
    expires_at: datetime | None = None
    enrich_async: bool = False


class KnowledgeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=1_000_000)
    tags: list[KnowledgeTag] = Field(default_factory=list, max_length=32)
    change_summary: str | None = Field(default=None, max_length=500)
    review_note: str | None = Field(default=None, max_length=2000)
    expires_at: datetime | None = None
    update_expires_at: bool = False
    enrich_async: bool = False


class KnowledgeTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_status: Literal["observation", "candidate", "accepted", "superseded"]
    expected_version: int = Field(ge=1)
    review_note: str | None = Field(default=None, max_length=2000)


class RetrievalSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    space_ids: list[str] | None = Field(default=None, max_length=100)
    active_space_id: str | None = Field(default=None, max_length=64)
    semantic_policy: Literal["prefer", "required", "disabled"] = "prefer"
    limit: int = Field(default=20, ge=1, le=50)
    tags: list[KnowledgeTag] | None = Field(default=None, max_length=32)


class RuntimeSettingsDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    reason: str = Field(min_length=5, max_length=500)
    values: RuntimeSettingsValues


class RuntimeSettingsActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_active_revision: int = Field(ge=0)
    reason: str = Field(min_length=5, max_length=500)


class RuntimeSettingsRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_active_revision: int = Field(ge=1)
    reason: str = Field(min_length=5, max_length=500)


class AIArchiveSpaceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1)


class AIListSpacesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=50, ge=1, le=100)


class AICreateSpaceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


class AIListSpaceMembersArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=50, ge=1, le=100)


class AISetSpaceMembershipArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(min_length=1, max_length=64)
    member_id: UUID
    role: SpaceRole | None
    expected_space_revision: int = Field(ge=1)


class AIKnowledgeSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4000)
    space_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(default=None, max_length=100)
    active_space_id: str | None = Field(default=None, max_length=64)
    semantic_policy: Literal["prefer", "required", "disabled"] = "prefer"
    limit: int = Field(default=20, ge=1, le=50)
    tags: list[Annotated[str, Field(min_length=1, max_length=80)]] | None = Field(default=None, max_length=32)


class AIKnowledgeReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: UUID


class AIKnowledgeCreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=1_000_000)
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=50)


class AIKnowledgeUpdateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: UUID
    expected_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=1_000_000)
    tags: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=50)
    change_summary: str | None = Field(default=None, max_length=500)


class AIKnowledgeArchiveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: UUID
    expected_version: int = Field(ge=1)


class AIListResourcesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str | None = Field(default=None, max_length=64)
    limit: int = Field(default=50, ge=1, le=100)


class AIIngestionJobActionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    expected_state: Literal["queued", "running", "retry_wait", "failed", "cancelled"]


class AISettingsInspectArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AISettingsProposeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    values: RuntimeSettingsValues
    reason: str = Field(min_length=5, max_length=500)


class AIToolExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict


AI_TOOL_DEFINITIONS = {
    "spaces.list.v1": ("spaces:read", "none", "List only active spaces accessible to the current member.", AIListSpacesArguments),
    "spaces.create.v1": ("spaces:write", "none", "Create a private space owned by the current member.", AICreateSpaceArguments),
    "spaces.archive.v1": ("spaces:write", "required", "Propose archiving an owned non-global space.", AIArchiveSpaceArguments),
    "spaces.members.list.v1": ("spaces:members", "none", "List members of an authorized space.", AIListSpaceMembersArguments),
    "spaces.members.set.v1": ("spaces:members", "required", "Propose adding, changing, or removing a space membership.", AISetSpaceMembershipArguments),
    "knowledge.search.v1": ("knowledge:read", "none", "Search knowledge across effective authorized spaces.", AIKnowledgeSearchArguments),
    "knowledge.read.v1": ("knowledge:read", "none", "Read an authorized knowledge item.", AIKnowledgeReadArguments),
    "knowledge.create.v1": ("knowledge:write", "none", "Create a versioned knowledge item in an authorized space.", AIKnowledgeCreateArguments),
    "knowledge.update.v1": ("knowledge:write", "none", "Update knowledge with optimistic concurrency.", AIKnowledgeUpdateArguments),
    "knowledge.archive.v1": ("knowledge:write", "required", "Propose archiving an authorized knowledge item.", AIKnowledgeArchiveArguments),
    "sources.list.v1": ("knowledge:read", "none", "List authorized source documents.", AIListResourcesArguments),
    "ingestion_jobs.list.v1": ("knowledge:read", "none", "List authorized durable ingestion jobs.", AIListResourcesArguments),
    "ingestion_jobs.cancel.v1": ("knowledge:write", "required", "Propose cancellation of an authorized ingestion job.", AIIngestionJobActionArguments),
    "ingestion_jobs.retry.v1": ("knowledge:write", "required", "Propose retry of an authorized terminal ingestion job.", AIIngestionJobActionArguments),
    "retrieval.explain.v1": ("knowledge:read", "none", "Inspect authorized retrieval results and bounded health explanations.", AIKnowledgeSearchArguments),
    "settings.inspect.v1": ("settings:read", "none", "Inspect the active allowlisted runtime settings.", AISettingsInspectArguments),
    "settings.propose.v1": ("settings:write", "required", "Propose a validated safe runtime settings draft.", AISettingsProposeArguments),
}


def _actor_id(principal: Principal) -> UUID:
    try:
        return UUID(principal.subject_id)
    except ValueError as exc:
        raise AuthorizationException() from exc

def _require_human_approval_principal(principal: Principal) -> None:
    if principal.kind is PrincipalKind.SESSION:
        return
    if principal.kind is PrincipalKind.API_KEY and principal.credential_id is not None:
        return
    raise AuthorizationException()


async def _family_id(session: AsyncSession, principal: Principal) -> UUID:
    if principal.kind is not PrincipalKind.SESSION or principal.credential_id is None:
        raise AuthenticationException("A website session is required.")
    try:
        credential_id = UUID(principal.credential_id)
    except ValueError as exc:
        raise AuthenticationException("A website session is required.") from exc
    credential = await session.get(SessionCredentialModel, credential_id)
    if credential is None or credential.revoked_at is not None:
        raise AuthenticationException("A website session is required.")
    return credential.family_id


def _contains_pattern(value: str) -> str:
    escaped = value.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _page_metadata(*, page: int, page_size: int, total_items: int) -> dict[str, int]:
    total_pages = (total_items + page_size - 1) // page_size if total_items else 0
    return {
        "page": min(page, total_pages) if total_pages else 1,
        "page_size": page_size,
        "total_items": total_items,
        "total_pages": total_pages,
    }


async def _paginate(
    session: AsyncSession,
    query,
    *,
    page: int,
    page_size: int,
    scalars: bool = False,
) -> tuple[list, dict[str, int]]:
    count_query = select(func.count()).select_from(query.order_by(None).subquery())
    total_items = int(await session.scalar(count_query) or 0)
    metadata = _page_metadata(page=page, page_size=page_size, total_items=total_items)
    if metadata["total_pages"] == 0:
        return [], metadata
    page_query = query.offset((metadata["page"] - 1) * page_size).limit(page_size)
    if scalars:
        items = list(await session.scalars(page_query))
    else:
        items = list((await session.execute(page_query)).all())
    return items, metadata


async def _reserve(
    session: AsyncSession,
    principal: Principal,
    operation: str,
    key: str,
    payload: dict,
):
    reservation = await IdempotencyService(session).reserve(
        actor_id=principal.subject_id,
        operation=operation,
        idempotency_key=key,
        payload=payload,
    )
    if reservation.status is ReservationStatus.IN_PROGRESS:
        raise ResourceConflictException("An identical request is still in progress.")
    return reservation


def _settings_payload(revision: RuntimeSettingsRevision) -> dict:
    values = revision.values
    serialized_values = (
        values.model_dump(mode="json")
        if isinstance(values, RuntimeSettingsValues)
        else RuntimeSettingsValues.model_validate(values).model_dump(mode="json")
    )
    return {
        "id": str(revision.id) if revision.id else None,
        "revision": revision.revision,
        "base_revision": revision.base_revision,
        "state": revision.state,
        "values": serialized_values,
        "created_at": revision.created_at.isoformat() if revision.created_at else None,
        "activated_at": revision.activated_at.isoformat() if revision.activated_at else None,
    }


async def _runtime_settings_dependency_probe(values: RuntimeSettingsValues) -> None:
    if values.retrieval.semantic_policy == "required":
        await default_retrieval_embedding_client().embed_query("runtime settings readiness")


def _knowledge_payload(
    item: DomainKnowledgeItem,
    *,
    include_content: bool = True,
    enrichment: str | None = None,
) -> dict:
    revision = item.current_revision
    content = revision.content if revision else item.content or ""
    payload = {
        "id": str(item.id),
        "space_id": item.workspace_id,
        "title": item.title,
        "content_excerpt": content[:320],
        "tags": list(revision.tags if revision else item.tags),
        "version": revision.version if revision else item.version,
        "lifecycle_status": item.lifecycle_status,
        "origin": item.origin,
        "source_detail": item.source_detail,
        "review_note": item.review_note,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "enrichment": enrichment,
    }
    if include_content:
        payload["content"] = content
    return payload


def _orm_knowledge_payload(item: KnowledgeItemModel, revision: KnowledgeRevisionModel | None, *, include_content: bool) -> dict:
    content = revision.content if revision else item.content
    payload = {
        "id": str(item.id),
        "space_id": item.workspace_id,
        "title": item.title,
        "content_excerpt": content[:320],
        "tags": list(revision.tags if revision else item.tags),
        "version": revision.version if revision else item.revision,
        "lifecycle_status": item.lifecycle_status,
        "origin": item.origin,
        "source_detail": item.source_detail,
        "review_note": item.review_note,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }
    if include_content:
        payload["content"] = content
    return payload


async def _upload_chunks(file: UploadFile):
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            return
        yield chunk


async def _active_policy(principal: Principal) -> EffectiveRuntimePolicy:
    return await load_active_runtime_policy(principal)


async def _record_upload_storage(principal: Principal, space_id: str, revision_id) -> None:
    try:
        factory = get_session_factory()
        async with factory() as session:
            revision = await session.get(DocumentRevisionModel, revision_id)
            size_bytes = int(revision.size_bytes or 0) if revision is not None else 0
        if size_bytes > 0:
            from src.gateway.presentation.quota_usage import record_storage_usage

            await record_storage_usage(principal, space_id=space_id, storage_bytes=size_bytes)
    except Exception:
        return


def _require_knowledge_tools(policy: EffectiveRuntimePolicy) -> None:
    if not policy.knowledge_tools_enabled:
        raise AuthorizationException()


def _require_mutation_tools(policy: EffectiveRuntimePolicy) -> None:
    if not policy.mutation_tools_enabled:
        raise AuthorizationException()


def _redact_explanation(result: dict) -> dict:
    result["health"] = {
        "semantic_status": result["health"]["semantic_status"],
        "degraded_reasons": [],
        "embedding_generation_id": None,
        "embedding_coverage": None,
    }
    result["explanation"] = {
        "effective_space_ids": [],
        "abstained": result["explanation"]["abstained"],
        "active_space_id": None,
    }
    return result


async def _ai_retrieval_result(
    principal: Principal,
    arguments: AIKnowledgeSearchArguments,
    policy: EffectiveRuntimePolicy,
) -> dict:
    default_ws = get_settings().gateway.default_workspace_id
    request_scope = resolve_request_space_scope(arguments.active_space_id, default_ws)
    result = await _retrieval_service().search(
        principal,
        arguments.query,
        requested_space_ids=narrow_requested_spaces(
            request_scope,
            set(arguments.space_ids) if arguments.space_ids is not None else None,
        ),
        active_space_id=arguments.active_space_id,
        semantic_policy=policy.effective_semantic_policy(arguments.semantic_policy),
        limit=arguments.limit,
        tags=list(arguments.tags) if arguments.tags is not None else None,
    )
    payload = {
        "query": result.query,
        "hits": [
            {
                "rank": hit.rank,
                "rank_score": hit.rank_score,
                "source_type": hit.candidate.source_type,
                "space_id": hit.candidate.space_id,
                "canonical_id": str(hit.candidate.canonical_id),
                "revision_id": str(hit.candidate.revision_id),
                "title": hit.candidate.title,
                "content_excerpt": hit.candidate.content[:600],
                "citation_uri": hit.candidate.citation_uri,
                "language": hit.candidate.language,
                "source_filename": hit.candidate.source_filename,
                "version": hit.candidate.version,
            }
            for hit in result.hits
        ],
        "health": {
            "semantic_status": result.health.semantic_status,
            "degraded_reasons": list(result.health.degraded_reasons),
            "embedding_generation_id": str(result.health.embedding_generation_id) if result.health.embedding_generation_id else None,
            "embedding_coverage": result.health.embedding_coverage,
        },
        "explanation": {
            "effective_space_ids": list(result.explanation.effective_space_ids),
            "abstained": result.explanation.abstained,
            "active_space_id": result.explanation.active_space_id,
        },
    }
    if not policy.retrieval_explanations_enabled:
        return _redact_explanation(payload)
    return payload


async def _ai_resource_space_ids(
    session: AsyncSession,
    principal: Principal,
    arguments: AIListResourcesArguments,
) -> tuple[str, ...]:
    if arguments.space_id is not None:
        await AuthorizationService(session).authorize_space(principal, arguments.space_id, Action.CONTENT_READ)
        return (arguments.space_id,)
    return await AuthorizationService(session).effective_space_ids(_actor_id(principal), principal=principal)


async def _ai_source_page(session: AsyncSession, principal: Principal, arguments: AIListResourcesArguments) -> dict:
    space_ids = await _ai_resource_space_ids(session, principal, arguments)
    if not space_ids:
        return {"items": [], **_page_metadata(page=1, page_size=arguments.limit, total_items=0)}
    query = (
        select(DocumentModel)
        .where(
            DocumentModel.archived_at.is_(None),
            DocumentModel.space_id.in_(space_ids),
        )
        .order_by(DocumentModel.created_at.desc(), DocumentModel.id.desc())
    )
    if arguments.space_id is not None:
        query = query.where(DocumentModel.space_id == arguments.space_id)
    documents, metadata = await _paginate(
        session,
        query,
        page=1,
        page_size=arguments.limit,
        scalars=True,
    )
    document_ids = [document.id for document in documents]
    revisions = list(
        await session.scalars(
            select(DocumentRevisionModel)
            .where(DocumentRevisionModel.document_id.in_(document_ids))
            .order_by(DocumentRevisionModel.document_id, DocumentRevisionModel.version.desc())
        )
    ) if document_ids else []
    latest: dict[UUID, DocumentRevisionModel] = {}
    for revision in revisions:
        latest.setdefault(revision.document_id, revision)
    return {
        "items": [
            {
                "id": str(document.id),
                "space_id": document.space_id,
                "display_name": document.display_name,
                "revision": document.revision,
                "status": latest[document.id].status if document.id in latest else "pending",
                "original_filename": latest[document.id].original_filename if document.id in latest else None,
                "size_bytes": latest[document.id].size_bytes if document.id in latest else None,
                "updated_at": document.updated_at.isoformat(),
            }
            for document in documents
        ],
        **metadata,
    }


async def _ai_job_page(session: AsyncSession, principal: Principal, arguments: AIListResourcesArguments) -> dict:
    space_ids = await _ai_resource_space_ids(session, principal, arguments)
    if not space_ids:
        return {"items": [], **_page_metadata(page=1, page_size=arguments.limit, total_items=0)}
    query = (
        select(IngestionJobModel)
        .where(IngestionJobModel.space_id.in_(space_ids))
        .order_by(IngestionJobModel.created_at.desc(), IngestionJobModel.id.desc())
    )
    if arguments.space_id is not None:
        query = query.where(IngestionJobModel.space_id == arguments.space_id)
    jobs, metadata = await _paginate(
        session,
        query,
        page=1,
        page_size=arguments.limit,
        scalars=True,
    )
    return {
        "items": [
            {
                "id": str(job.id),
                "space_id": job.space_id,
                "job_type": job.job_type,
                "document_id": str(job.document_id) if job.document_id is not None else None,
                "knowledge_item_id": str(job.knowledge_item_id) if job.knowledge_item_id is not None else None,
                "state": job.state,
                "progress": job.progress,
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
                "last_error_code": job.last_error_code,
                "updated_at": job.updated_at.isoformat(),
            }
            for job in jobs
        ],
        **metadata,
    }


@router.get("/ai-tools", response_model=AIToolList)
async def list_ai_tools(
    principal: Principal = Depends(require_principal),
) -> dict:
    policy = await _active_policy(principal)
    return {
        "items": [
            {
                "name": name,
                "description": description,
                "confirmation": (
                    "none"
                    if confirmation == "required" and not policy.destructive_tools_require_confirmation
                    else confirmation
                ),
                "parameters": argument_model.model_json_schema(),
            }
            for name, (scope, confirmation, description, argument_model) in AI_TOOL_DEFINITIONS.items()
            if ("*" in principal.scopes or scope in principal.scopes)
            and (name != "settings.propose.v1" or principal.system_role is SystemRole.SUPER_ADMIN)
            and _ai_tool_allowed(name, policy)
            and _profile_tool_visible(principal, name)
        ]
    }


def _ai_tool_allowed(tool_name: str, policy: EffectiveRuntimePolicy) -> bool:
    if tool_name in {"knowledge.search.v1", "retrieval.explain.v1", "knowledge.read.v1"}:
        if not policy.knowledge_tools_enabled:
            return False
    if tool_name == "retrieval.explain.v1":
        return policy.retrieval_explanations_enabled
    if tool_name in {
        "knowledge.create.v1",
        "knowledge.update.v1",
        "spaces.archive.v1",
        "spaces.members.set.v1",
        "knowledge.archive.v1",
        "ingestion_jobs.cancel.v1",
        "ingestion_jobs.retry.v1",
        "settings.propose.v1",
    }:
        return policy.mutation_tools_enabled
    return True


def _profile_tool_visible(principal: Principal, tool_name: str) -> bool:
    try:
        require_profile_tool(principal, tool_name)
    except AuthorizationException:
        return False
    return True


@router.post(
    "/ai-tools/{tool_name}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AIToolExecution,
    responses={200: {"model": AIToolExecution}, 201: {"model": AIToolExecution}},
)
async def execute_ai_tool(
    tool_name: str,
    payload: AIToolExecutionRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    definition = AI_TOOL_DEFINITIONS.get(tool_name)
    if definition is None:
        raise ValidationException("Unknown or unavailable AI tool.")
    require_profile_tool(principal, tool_name)
    scope, confirmation, _, argument_model = definition
    if "*" not in principal.scopes and scope not in principal.scopes:
        raise AuthorizationException()
    policy = await _active_policy(principal)
    if not _ai_tool_allowed(tool_name, policy):
        raise AuthorizationException()
    confirmation_required = (
        confirmation == "required" and policy.destructive_tools_require_confirmation
    )
    direct_destructive = (
        confirmation == "required"
        and not policy.destructive_tools_require_confirmation
        and principal.kind is PrincipalKind.SESSION
    )
    try:
        arguments = argument_model.model_validate(payload.arguments)
    except PydanticValidationError as exc:
        raise ValidationException("Invalid AI tool arguments.") from exc
    normalized = arguments.model_dump(mode="json")
    reservation = await _reserve(
        session,
        principal,
        f"ai_tool.{tool_name}",
        idempotency_key,
        normalized,
    )
    if reservation.status is ReservationStatus.REPLAY:
        increment_metric("gateway_tool_events_total", event="duplicate", outcome="replayed")
    if confirmation_required or (confirmation == "required" and not direct_destructive):
        if reservation.status is ReservationStatus.REPLAY:
            if not reservation.resource_ids:
                raise ResourceConflictException()
            action = await session.get(PendingAIActionModel, UUID(reservation.resource_ids[0]))
            if action is None:
                raise ResourceConflictException("The pending AI action is unavailable.")
            pending_id = action.id
            expires_at = action.expires_at
        else:
            pending = await AIManagementService(session).propose(
                principal,
                tool_name=tool_name,
                command=normalized,
                request_id=get_request_id(request),
            )
            pending_id = pending.action_id
            expires_at = pending.expires_at
            await IdempotencyService(session).complete(
                reservation.record_id,
                response_status=202,
                resource_ids=[str(pending_id)],
            )
            increment_metric("gateway_tool_events_total", event="confirmation", outcome="proposed")
        return {
            "status": "confirmation_required",
            "pending_action_id": str(pending_id),
            "tool_name": tool_name,
            "expires_at": expires_at.isoformat(),
        }
    if tool_name == "spaces.list.v1":
        rows, metadata = await _paginate(
            session,
            select(Workspace, SpaceMembershipModel.role)
            .join(SpaceMembershipModel, SpaceMembershipModel.space_id == Workspace.id)
            .where(
                SpaceMembershipModel.member_id == _actor_id(principal),
                Workspace.archived_at.is_(None),
            )
            .order_by(Workspace.name, Workspace.id),
            page=1,
            page_size=arguments.limit,
        )
        result = [
            {"id": space.id, "name": space.name, "role": role, "revision": space.revision}
            for space, role in rows
        ]
        if reservation.status is not ReservationStatus.REPLAY:
            await IdempotencyService(session).complete(
                reservation.record_id,
                response_status=200,
                resource_ids=[space["id"] for space in result],
            )
        response.status_code = status.HTTP_200_OK
        return {"items": result, **metadata}
    if tool_name == "spaces.create.v1":
        if reservation.status is ReservationStatus.REPLAY:
            space = await session.get(Workspace, reservation.resource_ids[0])
            if space is None:
                raise ResourceConflictException("The created space is unavailable.")
            result = {"id": space.id, "name": space.name, "role": "owner", "revision": space.revision}
        else:
            created = await SpaceService(session).create(
                principal,
                name=arguments.name,
                request_id=get_request_id(request),
            )
            result = {"id": created.space_id, "name": created.name, "role": "owner", "revision": created.revision}
            await IdempotencyService(session).complete(
                reservation.record_id,
                response_status=201,
                resource_ids=[created.space_id],
            )
        response.status_code = status.HTTP_201_CREATED
        return {"status": "executed", "tool_name": tool_name, "result": result}
    result: dict
    response_status = 200
    resource_ids: list[str] = []
    if tool_name == "spaces.members.list.v1":
        role = await AuthorizationService(session).authorize_space(principal, arguments.space_id, Action.MEMBERSHIP_MANAGE)
        if role is not SpaceRole.OWNER:
            raise AuthorizationException()
        rows, metadata = await _paginate(
            session,
            select(SpaceMembershipModel, MemberModel)
            .join(MemberModel, MemberModel.id == SpaceMembershipModel.member_id)
            .where(SpaceMembershipModel.space_id == arguments.space_id)
            .order_by(MemberModel.username_normalized),
            page=1,
            page_size=arguments.limit,
        )
        items = [
            {
                "member_id": str(member.id),
                "username": member.username,
                "display_name": member.display_name,
                "status": member.status,
                "role": membership.role,
            }
            for membership, member in rows
        ]
        result = {"items": items, **metadata}
        resource_ids = [item["member_id"] for item in items]
    elif tool_name in {"knowledge.search.v1", "retrieval.explain.v1"}:
        result = await _ai_retrieval_result(principal, arguments, policy)
        resource_ids = [hit["canonical_id"] for hit in result["hits"]]
    elif tool_name == "knowledge.read.v1":
        item = await KnowledgeManagementService(session).get(arguments.item_id)
        if item is None:
            raise AuthorizationException()
        await AuthorizationService(session).authorize_space(principal, item.workspace_id, Action.CONTENT_READ)
        result = _knowledge_payload(item)
        resource_ids = [str(item.id)]
    elif tool_name == "knowledge.create.v1":
        knowledge = KnowledgeManagementService(session)
        if reservation.status is ReservationStatus.REPLAY:
            if not reservation.resource_ids:
                raise ResourceConflictException()
            item = await knowledge.get(UUID(reservation.resource_ids[0]))
            if item is None:
                raise ResourceConflictException("The created knowledge item is unavailable.")
        else:
            item = await knowledge.create(
                principal,
                space_id=arguments.space_id,
                title=arguments.title.strip(),
                content=arguments.content,
                tags=[tag.strip() for tag in arguments.tags if tag.strip()],
                request_id=get_request_id(request),
            )
        result = _knowledge_payload(item)
        resource_ids = [str(item.id)]
        response_status = 201
    elif tool_name == "knowledge.update.v1":
        knowledge = KnowledgeManagementService(session)
        if reservation.status is ReservationStatus.REPLAY:
            if not reservation.resource_ids:
                raise ResourceConflictException()
            item = await knowledge.get(UUID(reservation.resource_ids[0]))
            if item is None:
                raise ResourceConflictException("The updated knowledge item is unavailable.")
        else:
            item = await knowledge.update(
                principal,
                arguments.item_id,
                expected_version=arguments.expected_version,
                title=arguments.title.strip(),
                content=arguments.content,
                tags=[tag.strip() for tag in arguments.tags if tag.strip()],
                change_summary=arguments.change_summary,
                request_id=get_request_id(request),
            )
        result = _knowledge_payload(item)
        resource_ids = [str(item.id)]
    elif tool_name == "sources.list.v1":
        result = await _ai_source_page(session, principal, arguments)
        resource_ids = [item["id"] for item in result["items"]]
    elif tool_name == "ingestion_jobs.list.v1":
        result = await _ai_job_page(session, principal, arguments)
        resource_ids = [item["id"] for item in result["items"]]
    elif tool_name == "settings.inspect.v1":
        result = _settings_payload(await RuntimeSettingsService(session).active())
        if result["id"] is not None:
            resource_ids = [result["id"]]
    elif tool_name in {
        "spaces.archive.v1",
        "spaces.members.set.v1",
        "knowledge.archive.v1",
        "ingestion_jobs.cancel.v1",
        "ingestion_jobs.retry.v1",
        "settings.propose.v1",
    }:
        if not direct_destructive:
            raise AuthorizationException()
        if reservation.status is ReservationStatus.REPLAY:
            if not reservation.resource_ids:
                raise ResourceConflictException()
            action = await session.get(PendingAIActionModel, UUID(reservation.resource_ids[0]))
            if action is None:
                raise ResourceConflictException("The pending AI action is unavailable.")
        else:
            manager = AIManagementService(session)
            pending = await manager.propose(
                principal,
                tool_name=tool_name,
                command=normalized,
                request_id=get_request_id(request),
            )
            action = await manager.confirm_and_execute(
                principal,
                pending.action_id,
                request_id=get_request_id(request),
            )
            await IdempotencyService(session).complete(
                reservation.record_id,
                response_status=200,
                resource_ids=[str(action.id)],
            )
            increment_metric("gateway_tool_events_total", event="direct", outcome="success")
        result = {"pending_action_id": str(action.id), "status": action.state}
        resource_ids = [str(action.id)]
    else:
        raise ValidationException("Unknown or unavailable AI tool.")
    if reservation.status is not ReservationStatus.REPLAY:
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=response_status,
            resource_ids=resource_ids,
        )
        increment_metric("gateway_tool_events_total", event="direct", outcome="success")
    response.status_code = response_status
    return {
        "status": "executed",
        "tool_name": tool_name,
        "result": result,
    }


@router.post("/ai-actions/{action_id}/confirm", response_model=ConfirmedAIAction)
async def confirm_ai_action(
    action_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    _require_human_approval_principal(principal)
    reservation = await _reserve(
        session,
        principal,
        "ai_action.confirm",
        idempotency_key,
        {"action_id": str(action_id)},
    )
    if reservation.status is ReservationStatus.REPLAY:
        action = await session.get(PendingAIActionModel, action_id)
        if action is None or action.actor_member_id != _actor_id(principal):
            raise AuthorizationException()
    else:
        action = await AIManagementService(session).confirm_and_execute(
            principal,
            action_id,
            request_id=get_request_id(request),
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=[str(action_id)],
        )
    return {
        "pending_action_id": str(action.id),
        "status": action.state,
        "tool_name": action.tool_name,
    }


@router.get("/ai-actions", response_model=Page[PendingAIAction])
async def list_ai_actions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    _require_human_approval_principal(principal)
    current_time = datetime.now(timezone.utc)
    query = (
        select(PendingAIActionModel)
        .where(
            PendingAIActionModel.actor_member_id == _actor_id(principal),
            PendingAIActionModel.state == "pending",
            PendingAIActionModel.expires_at > current_time,
        )
        .order_by(PendingAIActionModel.created_at.desc(), PendingAIActionModel.id.desc())
    )
    actions, metadata = await _paginate(
        session,
        query,
        page=page,
        page_size=page_size,
        scalars=True,
    )
    return {
        "items": [
            {
                "id": str(action.id),
                "tool_name": action.tool_name,
                "target_ids": action.target_ids,
                "expected_revision": action.expected_revision,
                "created_at": action.created_at.isoformat(),
                "expires_at": action.expires_at.isoformat(),
                "status": action.state,
            }
            for action in actions
        ],
        **metadata,
    }


@router.get("/ai-actions/{action_id}", response_model=PendingAIActionDetail)
async def review_ai_action(
    action_id: UUID,
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    _require_human_approval_principal(principal)
    return await review_pending_action(session, principal, action_id)


@router.get("/me", response_model=CurrentMember)
async def me(
    principal: Principal = Depends(require_scope("spaces:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    member = await session.get(MemberModel, _actor_id(principal))
    if member is None or member.status == MemberStatus.DISABLED.value:
        raise AuthorizationException()
    mfa_enabled = bool(await session.scalar(select(exists().where(
        MFAFactorModel.member_id == member.id,
        MFAFactorModel.confirmed_at.is_not(None),
        MFAFactorModel.retired_at.is_(None),
    ))))
    return {
        "id": str(member.id),
        "username": member.username,
        "display_name": member.display_name,
        "status": member.status,
        "system_role": member.system_role,
        "requires_password_change": member.force_password_change,
        "mfa_enabled": mfa_enabled,
    }


@router.get("/capabilities", response_model=ClientCapabilities)
async def capabilities(
    principal: Principal = Depends(require_principal),
) -> dict:
    gateway = get_settings().gateway
    return {
        "max_upload_bytes": gateway.max_upload_bytes,
        "max_request_body_bytes": gateway.max_request_body_bytes,
    }


@router.get("/quotas/usage", response_model=CredentialQuotaUsage)
async def quota_usage(
    space_id: str = Query(default="global", min_length=1, max_length=64),
    principal: Principal = Depends(require_principal),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    await AuthorizationService(session).authorize_space(principal, space_id, Action.CONTENT_READ)
    service = quota_service_from_settings(get_settings().gateway)
    observed = await service.usage(session, principal, space_id=space_id)
    if observed is None:
        return {
            "credential_id": principal.credential_id,
            "space_id": space_id,
            "window_started_at": None,
            "request_count": 0,
            "token_count": 0,
            "storage_bytes": 0,
            "requests_limit": service.policy.requests_per_minute,
            "tokens_limit": service.policy.tokens_per_minute,
            "storage_limit": service.policy.storage_bytes,
            "concurrent_limit": service.policy.concurrent_requests,
            "concurrent_in_flight": await service.in_flight(principal, space_id=space_id),
        }
    return {
        "credential_id": observed.credential_id,
        "space_id": observed.space_id,
        "window_started_at": observed.window_started_at.isoformat() if observed.window_started_at else None,
        "request_count": observed.request_count,
        "token_count": observed.token_count,
        "storage_bytes": observed.storage_bytes,
        "requests_limit": observed.requests_limit,
        "tokens_limit": observed.tokens_limit,
        "storage_limit": observed.storage_limit,
        "concurrent_limit": observed.concurrent_limit,
        "concurrent_in_flight": observed.concurrent_in_flight,
    }


@router.get("/spaces", response_model=Page[SpaceSummary])
async def list_spaces(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    q: str | None = Query(default=None, min_length=1, max_length=255),
    principal: Principal = Depends(require_scope("spaces:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "space.list")
    member_id = _actor_id(principal)
    query = (
        select(Workspace, SpaceMembershipModel.role)
        .join(SpaceMembershipModel, SpaceMembershipModel.space_id == Workspace.id)
        .where(
            SpaceMembershipModel.member_id == member_id,
            Workspace.archived_at.is_(None),
        )
        .order_by(Workspace.id)
    )
    if q is not None and q.strip():
        pattern = _contains_pattern(q)
        query = query.where(or_(Workspace.id.ilike(pattern, escape="\\"), Workspace.name.ilike(pattern, escape="\\")))
    rows, metadata = await _paginate(session, query, page=page, page_size=page_size)
    return {
        "items": [
            {
                "id": space.id,
                "name": space.name,
                "role": role,
                "personal": role == SpaceRole.OWNER.value and space.id != "global",
                "revision": space.revision,
                "created_at": space.created_at.isoformat(),
            }
            for space, role in rows
        ],
        **metadata,
    }


@router.get("/admin/spaces", response_model=Page[AdminSpaceSummary])
async def list_admin_spaces(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    q: str | None = Query(default=None, min_length=1, max_length=255),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    if principal.system_role is not SystemRole.SUPER_ADMIN:
        raise AuthorizationException()
    query = (
        select(Workspace, SpaceMembershipModel, MemberModel)
        .join(
            SpaceMembershipModel,
            and_(
                SpaceMembershipModel.space_id == Workspace.id,
                SpaceMembershipModel.role == SpaceRole.OWNER.value,
            ),
        )
        .join(MemberModel, MemberModel.id == SpaceMembershipModel.member_id)
        .where(Workspace.archived_at.is_(None))
        .order_by(Workspace.id)
    )
    if q is not None and q.strip():
        pattern = _contains_pattern(q)
        query = query.where(
            or_(
                Workspace.id.ilike(pattern, escape="\\"),
                Workspace.name.ilike(pattern, escape="\\"),
                MemberModel.username.ilike(pattern, escape="\\"),
                MemberModel.display_name.ilike(pattern, escape="\\"),
            )
        )
    rows, metadata = await _paginate(session, query, page=page, page_size=page_size)
    return {
        "items": [
            {
                "id": space.id,
                "name": space.name,
                "revision": space.revision,
                "owner_member_id": str(owner.id),
                "owner_username": owner.username,
                "owner_display_name": owner.display_name,
                "created_at": space.created_at,
            }
            for space, _, owner in rows
        ],
        **metadata,
    }


@router.post("/spaces", status_code=status.HTTP_201_CREATED, response_model=CreatedSpace)
async def create_space(
    payload: SpaceCreateRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("spaces:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "space.create")
    reservation = await _reserve(
        session,
        principal,
        "space.create",
        idempotency_key,
        payload.model_dump(mode="json"),
    )
    if reservation.status is ReservationStatus.REPLAY:
        if not reservation.resource_ids:
            raise ResourceConflictException()
        space = await session.get(Workspace, reservation.resource_ids[0])
        if space is None:
            raise ResourceConflictException()
        return {
            "id": space.id,
            "name": space.name,
            "role": SpaceRole.OWNER.value,
            "revision": space.revision,
        }
    try:
        created = await SpaceService(session).create(
            principal,
            name=payload.name,
            request_id=get_request_id(request),
        )
    except ValueError as exc:
        raise ValidationException(str(exc)) from exc
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=201,
        resource_ids=[created.space_id],
    )
    return {
        "id": created.space_id,
        "name": created.name,
        "role": SpaceRole.OWNER.value,
        "revision": created.revision,
    }


async def _space_detail(session: AsyncSession, principal: Principal, space_id: str) -> dict:
    row = (
        await session.execute(
            select(SpaceMembershipModel, Workspace)
            .join(Workspace, Workspace.id == SpaceMembershipModel.space_id)
            .where(
                SpaceMembershipModel.space_id == space_id,
                SpaceMembershipModel.member_id == _actor_id(principal),
                Workspace.archived_at.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise AuthorizationException()
    space_membership, space = row
    policy = await effective_chunk_policy(session, space_id)
    return {
        "id": space.id,
        "name": space.name,
        "role": space_membership.role,
        "revision": space.revision,
        "personal": space_membership.role == SpaceRole.OWNER.value and space.id != "global",
        "created_at": space.created_at.isoformat(),
        "chunk_policy": chunk_policy_payload(policy),
    }


@router.get("/spaces/{space_id}", response_model=SpaceDetail)
async def get_space(
    space_id: str,
    principal: Principal = Depends(require_scope("spaces:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "space.list")
    return await _space_detail(session, principal, space_id)


@router.put("/spaces/{space_id}/chunk-policy", response_model=SpaceDetail)
async def update_space_chunk_policy(
    space_id: str,
    payload: ChunkPolicyRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("spaces:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "space.chunk_policy")
    validate_chunk_policy(
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap,
        chunk_strategy=payload.chunk_strategy,
    )
    reservation = await _reserve(
        session,
        principal,
        "space.chunk_policy.update",
        idempotency_key,
        {
            "space_id": space_id,
            "chunk_size": payload.chunk_size,
            "chunk_overlap": payload.chunk_overlap,
            "chunk_strategy": payload.chunk_strategy,
        },
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await SpaceService(session).update_chunk_policy(
            principal,
            space_id,
            chunk_size=payload.chunk_size,
            chunk_overlap=payload.chunk_overlap,
            chunk_strategy=payload.chunk_strategy,
            request_id=get_request_id(request),
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=[space_id],
        )
    return await _space_detail(session, principal, space_id)


@router.get("/spaces/{space_id}/members", response_model=Page[SpaceMember])
async def list_space_members(
    space_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    q: str | None = Query(default=None, min_length=1, max_length=255),
    role: Literal["owner", "editor", "reader"] | None = Query(default=None),
    principal: Principal = Depends(require_scope("spaces:members")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "space.members.list")
    actor_membership = await session.scalar(
        select(SpaceMembershipModel).where(
            SpaceMembershipModel.space_id == space_id,
            SpaceMembershipModel.member_id == _actor_id(principal),
        )
    )
    if actor_membership is None or actor_membership.role != SpaceRole.OWNER.value:
        raise AuthorizationException()
    query = (
        select(SpaceMembershipModel, MemberModel)
        .join(MemberModel, MemberModel.id == SpaceMembershipModel.member_id)
        .where(SpaceMembershipModel.space_id == space_id)
        .order_by(MemberModel.username_normalized)
    )
    if q is not None and q.strip():
        pattern = _contains_pattern(q)
        query = query.where(
            or_(
                MemberModel.username.ilike(pattern, escape="\\"),
                MemberModel.username_normalized.ilike(pattern, escape="\\"),
                MemberModel.display_name.ilike(pattern, escape="\\"),
            )
        )
    if role is not None:
        query = query.where(SpaceMembershipModel.role == role)
    rows, metadata = await _paginate(session, query, page=page, page_size=page_size)
    return {
        "items": [
            {
                "member_id": str(member.id),
                "username": member.username,
                "display_name": member.display_name,
                "status": member.status,
                "role": membership.role,
                "updated_at": membership.updated_at.isoformat(),
            }
            for membership, member in rows
        ],
        **metadata,
    }


@router.get("/spaces/{space_id}/member-candidates", response_model=Page[SpaceMemberCandidate])
async def list_space_member_candidates(
    space_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    q: str | None = Query(default=None, min_length=1, max_length=255),
    principal: Principal = Depends(require_scope("spaces:members")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "space.members.list")
    actor_membership = await session.scalar(
        select(SpaceMembershipModel).where(
            SpaceMembershipModel.space_id == space_id,
            SpaceMembershipModel.member_id == _actor_id(principal),
        )
    )
    if actor_membership is None or actor_membership.role != SpaceRole.OWNER.value:
        raise AuthorizationException()
    query = (
        select(MemberModel)
        .where(
            MemberModel.status == MemberStatus.ACTIVE.value,
            ~exists().where(
                SpaceMembershipModel.space_id == space_id,
                SpaceMembershipModel.member_id == MemberModel.id,
            ),
        )
        .order_by(MemberModel.username_normalized)
    )
    if q is not None and q.strip():
        pattern = _contains_pattern(q)
        query = query.where(
            or_(
                MemberModel.username.ilike(pattern, escape="\\"),
                MemberModel.username_normalized.ilike(pattern, escape="\\"),
                MemberModel.display_name.ilike(pattern, escape="\\"),
            )
        )
    rows, metadata = await _paginate(
        session,
        query,
        page=page,
        page_size=page_size,
        scalars=True,
    )
    return {
        "items": [
            {
                "member_id": str(member.id),
                "username": member.username,
                "display_name": member.display_name,
            }
            for member in rows
        ],
        **metadata,
    }


@router.put("/spaces/{space_id}/members/{member_id}", response_model=SpaceMembership)
async def set_space_membership(
    space_id: str,
    member_id: UUID,
    payload: MembershipRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("spaces:members")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "space.members.set")
    reservation = await _reserve(
        session,
        principal,
        "space.membership.set",
        idempotency_key,
        {"space_id": space_id, "member_id": str(member_id), "role": payload.role},
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await SpaceService(session).set_membership(
            principal,
            space_id,
            member_id,
            role=SpaceRole(payload.role),
            request_id=get_request_id(request),
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=[space_id, str(member_id)],
        )
    return {"space_id": space_id, "member_id": str(member_id), "role": payload.role}


@router.put("/spaces/{space_id}/ownership", response_model=SpaceMembership)
async def transfer_space_ownership(
    space_id: str,
    payload: OwnershipTransferRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("spaces:members")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "space.members.set")
    reservation = await _reserve(
        session,
        principal,
        "space.ownership.transfer",
        idempotency_key,
        {
            "space_id": space_id,
            "target_member_id": str(payload.target_member_id),
            "expected_revision": payload.expected_revision,
        },
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await SpaceService(session).transfer_ownership(
            principal,
            await _family_id(session, principal),
            space_id,
            payload.target_member_id,
            expected_revision=payload.expected_revision,
            request_id=get_request_id(request),
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=[space_id, str(payload.target_member_id)],
        )
    return {
        "space_id": space_id,
        "member_id": str(payload.target_member_id),
        "role": SpaceRole.OWNER.value,
    }


@router.put("/admin/spaces/{space_id}/ownership", response_model=SpaceMembership)
async def emergency_transfer_space_ownership(
    space_id: str,
    payload: EmergencyOwnershipTransferRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    reservation = await _reserve(
        session,
        principal,
        "space.ownership.emergency_transfer",
        idempotency_key,
        {
            "space_id": space_id,
            "target_member_id": str(payload.target_member_id),
            "expected_revision": payload.expected_revision,
            "reason": payload.reason.strip(),
        },
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await SpaceService(session).transfer_ownership(
            principal,
            await _family_id(session, principal),
            space_id,
            payload.target_member_id,
            expected_revision=payload.expected_revision,
            request_id=get_request_id(request),
            emergency_reason=payload.reason,
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=[space_id, str(payload.target_member_id)],
        )
    return {
        "space_id": space_id,
        "member_id": str(payload.target_member_id),
        "role": SpaceRole.OWNER.value,
    }


@router.delete("/spaces/{space_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_space_membership(
    space_id: str,
    member_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("spaces:members")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    reservation = await _reserve(
        session,
        principal,
        "space.membership.remove",
        idempotency_key,
        {"space_id": space_id, "member_id": str(member_id)},
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await SpaceService(session).set_membership(
            principal,
            space_id,
            member_id,
            role=None,
            request_id=get_request_id(request),
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=204,
            resource_ids=[space_id, str(member_id)],
        )
    return Response(status_code=204)


@router.delete("/spaces/{space_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_space(
    space_id: str,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("spaces:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    require_profile_route(principal, "space.archive")
    reservation = await _reserve(
        session,
        principal,
        "space.archive",
        idempotency_key,
        {"space_id": space_id},
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await SpaceService(session).archive(
            principal,
            space_id,
            request_id=get_request_id(request),
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=204,
            resource_ids=[space_id],
        )
    return Response(status_code=204)


@router.get("/knowledge/export", response_model=KnowledgeExport)
async def export_knowledge(
    space_id: str = Query(min_length=1, max_length=64),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.export")
    from src.gateway.application.services.knowledge_export_service import KnowledgeExportService

    return await KnowledgeExportService(session).export_space(
        UseCaseContext(principal=principal),
        space_id,
    )


class KnowledgeImportRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    format: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    space_id: str = Field(min_length=1, max_length=64)
    items: list[dict] = Field(default_factory=list, max_length=5000)


@router.post("/knowledge/import", response_model=KnowledgeImportSummary)
async def import_knowledge(
    payload: KnowledgeImportRequest,
    request: Request,
    space_id: str = Query(min_length=1, max_length=64),
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.import")
    from src.gateway.application.services.knowledge_export_service import (
        KnowledgeExportService,
        import_summary_payload,
    )

    reservation = await _reserve(
        session,
        principal,
        "knowledge.import",
        idempotency_key,
        {"space_id": space_id, "document": payload.model_dump()},
    )
    if reservation.status is ReservationStatus.REPLAY:
        if not reservation.resource_ids:
            raise ResourceConflictException()
        replayed = await KnowledgeExportService(session).import_space(
            UseCaseContext(
                principal=principal,
                policy=await _active_policy(principal),
                request_id=get_request_id(request),
                idempotency_key=idempotency_key,
            ),
            space_id,
            payload.model_dump(),
        )
        return import_summary_payload(replayed)
    result = await KnowledgeExportService(session).import_space(
        UseCaseContext(
            principal=principal,
            policy=await _active_policy(principal),
            request_id=get_request_id(request),
            idempotency_key=idempotency_key,
        ),
        space_id,
        payload.model_dump(),
    )
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=200,
        resource_ids=[space_id],
    )
    return import_summary_payload(result)


@router.get("/knowledge", response_model=Page[KnowledgeSummary])
async def list_knowledge(
    space_id: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    q: str | None = Query(default=None, min_length=1, max_length=500),
    tag: str | None = Query(default=None, min_length=1, max_length=80),
    lifecycle_status: Literal["observation", "candidate", "accepted", "superseded"] | None = Query(default=None),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.search")
    effective_space_ids = await AuthorizationService(session).effective_space_ids(_actor_id(principal), principal=principal)
    if space_id is not None:
        if space_id not in effective_space_ids:
            return {"items": [], **_page_metadata(page=page, page_size=page_size, total_items=0)}
        scoped_spaces: tuple[str, ...] = (space_id,)
    else:
        scoped_spaces = effective_space_ids
        if not scoped_spaces:
            return {"items": [], **_page_metadata(page=page, page_size=page_size, total_items=0)}
    query = (
        select(KnowledgeItemModel, KnowledgeRevisionModel)
        .outerjoin(KnowledgeRevisionModel, KnowledgeRevisionModel.id == KnowledgeItemModel.current_revision_id)
        .where(
            KnowledgeItemModel.is_deleted.is_(False),
            KnowledgeItemModel.workspace_id.in_(scoped_spaces),
        )
        .order_by(KnowledgeItemModel.updated_at.desc(), KnowledgeItemModel.id.desc())
    )
    if q is not None and q.strip():
        pattern = _contains_pattern(q)
        query = query.where(
            or_(
                KnowledgeItemModel.title.ilike(pattern, escape="\\"),
                KnowledgeItemModel.content.ilike(pattern, escape="\\"),
                KnowledgeRevisionModel.title.ilike(pattern, escape="\\"),
                KnowledgeRevisionModel.content.ilike(pattern, escape="\\"),
            )
        )
    if tag is not None and tag.strip():
        query = query.where(KnowledgeItemModel.tags.contains([tag.strip()]))
    if lifecycle_status is not None:
        query = query.where(KnowledgeItemModel.lifecycle_status == lifecycle_status)
    rows, metadata = await _paginate(session, query, page=page, page_size=page_size)
    return {
        "items": [_orm_knowledge_payload(item, revision, include_content=False) for item, revision in rows],
        **metadata,
    }


@router.post("/knowledge", status_code=status.HTTP_201_CREATED, response_model=KnowledgeEnrichmentReceipt | KnowledgeDetail, responses={202: {"model": KnowledgeEnrichmentReceipt}})
async def create_knowledge(
    payload: KnowledgeCreateRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.create")
    outcome = await KnowledgeCommands(session).create(
        UseCaseContext(
            principal=principal,
            policy=await _active_policy(principal),
            request_id=get_request_id(request),
            idempotency_key=idempotency_key,
        ),
        CreateKnowledgeCommand(
            space_id=payload.space_id,
            title=payload.title,
            content=payload.content,
            tags=tuple(payload.tags),
            lifecycle_status=payload.lifecycle_status,
            origin=payload.origin,
            source_detail=payload.source_detail,
            expires_at=payload.expires_at,
            enrich_async=payload.enrich_async,
        ),
    )
    assert outcome.value is not None
    if payload.enrich_async and not outcome.replayed:
        service = KnowledgeManagementService(session)
        revision = outcome.value.current_revision
        assert revision is not None
        job_id = await service.enrichment_job_for_revision(revision.id)
        assert job_id is not None
        response.status_code = status.HTTP_202_ACCEPTED
        return {
            **_knowledge_payload(outcome.value, enrichment="pending"),
            "job_id": str(job_id),
            "job_state": "queued",
        }
    if outcome.replayed:
        revision = outcome.value.current_revision
        enrichment = None
        if revision is not None:
            enrichment = await KnowledgeManagementService(session).enrichment_state(revision.id)
        return _knowledge_payload(outcome.value, enrichment=enrichment)
    return _knowledge_payload(outcome.value, enrichment=None)


def _stale_threshold_days(older_than_days: int | None) -> int:
    if older_than_days is not None:
        return older_than_days
    return get_settings().gateway.stale_after_days


@router.get("/knowledge/stale", response_model=Page[StaleKnowledgeItem])
async def list_stale_knowledge(
    space_id: str | None = Query(default=None, max_length=64),
    older_than_days: int | None = Query(default=None, ge=1, le=3650),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.stale")
    effective_space_ids = await AuthorizationService(session).effective_space_ids(_actor_id(principal), principal=principal)
    if space_id is not None:
        if space_id not in effective_space_ids:
            return {"items": [], **_page_metadata(page=page, page_size=page_size, total_items=0)}
        scoped_spaces: tuple[str, ...] = (space_id,)
    else:
        scoped_spaces = effective_space_ids
        if not scoped_spaces:
            return {"items": [], **_page_metadata(page=page, page_size=page_size, total_items=0)}
    threshold_days = _stale_threshold_days(older_than_days)
    cutoff = datetime.now(timezone.utc) - timedelta(days=threshold_days)
    query = (
        select(KnowledgeItemModel, KnowledgeRevisionModel)
        .outerjoin(KnowledgeRevisionModel, KnowledgeRevisionModel.id == KnowledgeItemModel.current_revision_id)
        .where(
            KnowledgeItemModel.is_deleted.is_(False),
            KnowledgeItemModel.archived_at.is_(None),
            KnowledgeItemModel.workspace_id.in_(scoped_spaces),
            KnowledgeItemModel.updated_at < cutoff,
        )
        .order_by(KnowledgeItemModel.updated_at.asc(), KnowledgeItemModel.id.asc())
    )
    rows, metadata = await _paginate(session, query, page=page, page_size=page_size)
    observed = datetime.now(timezone.utc)
    return {
        "items": [
            {
                "id": str(item.id),
                "space_id": item.workspace_id,
                "title": item.title,
                "version": revision.version if revision else item.revision,
                "updated_at": item.updated_at.isoformat(),
                "age_days": max(0, int((observed - item.updated_at).total_seconds() // 86400)),
            }
            for item, revision in rows
        ],
        **metadata,
    }


@router.get("/knowledge/{item_id}", response_model=KnowledgeDetail)
async def get_knowledge(
    item_id: UUID,
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.read")
    item = await KnowledgeCommands(session).get(
        UseCaseContext(principal=principal),
        item_id,
    )
    revision = item.current_revision
    enrichment = None
    if revision is not None:
        enrichment = await KnowledgeManagementService(session).enrichment_state(revision.id)
    return _knowledge_payload(item, enrichment=enrichment)


@router.put("/knowledge/{item_id}", response_model=KnowledgeEnrichmentReceipt | KnowledgeDetail, responses={202: {"model": KnowledgeEnrichmentReceipt}})
async def update_knowledge(
    item_id: UUID,
    payload: KnowledgeUpdateRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.update")
    outcome = await KnowledgeCommands(session).update(
        UseCaseContext(
            principal=principal,
            policy=await _active_policy(principal),
            request_id=get_request_id(request),
            idempotency_key=idempotency_key,
        ),
        UpdateKnowledgeCommand(
            item_id=item_id,
            expected_version=payload.expected_version,
            title=payload.title,
            content=payload.content,
            tags=tuple(payload.tags),
            change_summary=payload.change_summary,
            review_note=payload.review_note,
            expires_at=payload.expires_at,
            update_expires_at=payload.update_expires_at,
            enrich_async=payload.enrich_async,
        ),
    )
    assert outcome.value is not None
    if payload.enrich_async and not outcome.replayed:
        service = KnowledgeManagementService(session)
        revision = outcome.value.current_revision
        assert revision is not None
        job_id = await service.enrichment_job_for_revision(revision.id)
        assert job_id is not None
        response.status_code = status.HTTP_202_ACCEPTED
        return {
            **_knowledge_payload(outcome.value, enrichment="pending"),
            "job_id": str(job_id),
            "job_state": "queued",
        }
    revision = outcome.value.current_revision
    enrichment = None
    if revision is not None:
        enrichment = await KnowledgeManagementService(session).enrichment_state(revision.id)
    return _knowledge_payload(outcome.value, enrichment=enrichment)


@router.post("/knowledge/{item_id}/transitions", response_model=KnowledgeTransition)
async def transition_knowledge(
    item_id: UUID,
    payload: KnowledgeTransitionRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.transition")
    outcome = await KnowledgeCommands(session).transition(
        UseCaseContext(
            principal=principal,
            policy=await _active_policy(principal),
            request_id=get_request_id(request),
            idempotency_key=idempotency_key,
        ),
        TransitionKnowledgeCommand(
            item_id=item_id,
            to_status=payload.to_status,
            expected_version=payload.expected_version,
            review_note=payload.review_note,
        ),
    )
    assert outcome.value is not None
    transitioned, from_status = outcome.value
    revision = transitioned.current_revision
    return {
        "id": str(transitioned.id),
        "from_status": from_status or transitioned.lifecycle_status,
        "to_status": transitioned.lifecycle_status,
        "version": revision.version if revision else transitioned.version,
    }


@router.delete("/knowledge/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge(
    item_id: UUID,
    request: Request,
    expected_version: int = Query(ge=1),
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    require_profile_route(principal, "knowledge.delete")
    await KnowledgeCommands(session).delete(
        UseCaseContext(
            principal=principal,
            policy=await _active_policy(principal),
            request_id=get_request_id(request),
            idempotency_key=idempotency_key,
        ),
        DeleteKnowledgeCommand(item_id=item_id, expected_version=expected_version),
    )
    return Response(status_code=204)


@router.post("/knowledge/{item_id}/reviewed", response_model=ReviewedKnowledge)
async def mark_knowledge_reviewed(
    item_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.reviewed")
    item = await session.get(KnowledgeItemModel, item_id)
    if item is None or item.is_deleted:
        raise AuthorizationException()
    await AuthorizationService(session).authorize_space(principal, item.workspace_id, Action.CONTENT_WRITE)
    reservation = await _reserve(
        session,
        principal,
        "knowledge.reviewed",
        idempotency_key,
        {"item_id": str(item_id)},
    )
    if reservation.status is not ReservationStatus.REPLAY:
        AuditService(AuditRepository(session)).record(
            actor_member_id=_actor_id(principal),
            actor_kind=principal.kind.value,
            request_id=get_request_id(request),
            action="knowledge.reviewed",
            resource_type="knowledge_item",
            resource_id=str(item_id),
            details={"space_id": item.workspace_id, "version": item.revision},
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=[str(item_id)],
        )
    return {"id": str(item_id), "status": "reviewed"}


def _reembed_service() -> EmbeddingReembedService:
    factory = get_session_factory()
    return EmbeddingReembedService(
        factory,
        default_retrieval_embedding_client(),
        EmbeddingGenerationService(factory),
    )


def _reindex_targets_payload(progress) -> list[dict]:
    return [
        {
            "name": item.table,
            "phase": item.phase,
            "rows_migrated": item.rows_migrated,
            "completed": item.completed,
            "pending": not item.completed,
        }
        for item in progress
    ]


class ReindexEnqueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[str] | None = Field(default=None, max_length=16)


class GenerationPromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = False
    reason: str | None = Field(default=None, max_length=500)


class GenerationRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


async def _require_reindex_operator(session: AsyncSession, principal: Principal) -> None:
    space_ids = await AuthorizationService(session).effective_space_ids(_actor_id(principal), principal=principal)
    for space_id in space_ids:
        try:
            role = await AuthorizationService(session).authorize_space(principal, space_id, Action.CONTENT_WRITE)
        except AuthorizationException:
            continue
        if role in (SpaceRole.OWNER, SpaceRole.EDITOR):
            return
    raise AuthorizationException()


@router.get("/operations/reindex", response_model=ReindexStatus)
async def reindex_status(
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "operations.reindex.read")
    return await EmbeddingGenerationLifecycleService(session).status_payload()


@router.post("/operations/reindex/enqueue", response_model=ReindexEnqueueResult)
async def enqueue_reindex(
    payload: ReindexEnqueueRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "operations.reindex.mutate")
    _require_mutation_tools(await _active_policy(principal))
    await _require_reindex_operator(session, principal)
    known = [target.name for target in REEMBED_TARGETS]
    requested = tuple(payload.targets) if payload.targets is not None else tuple(known)
    unknown = [name for name in requested if name not in known]
    if unknown:
        raise ValidationException(f"Unknown reindex targets: {', '.join(sorted(unknown))}")
    if not requested:
        raise ValidationException("At least one reindex target is required.")
    reservation = await _reserve(
        session,
        principal,
        "operations.reindex.enqueue",
        idempotency_key,
        {"targets": sorted(requested)},
    )
    if reservation.status is ReservationStatus.REPLAY:
        progress = await _reembed_service().status()
        selected = [item for item in progress if item.table in set(requested)]
    else:
        progress = await _reembed_service().enqueue(targets=requested)
        selected = [item for item in progress if item.table in set(requested)]
        AuditService(AuditRepository(session)).record(
            actor_member_id=_actor_id(principal),
            actor_kind=principal.kind.value,
            request_id=get_request_id(request),
            action="operations.reindex.enqueue_requested",
            resource_type="embedding_generation",
            resource_id=None,
            details={"targets": sorted(requested)},
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=sorted(requested),
        )
    return {"status": "enqueued", "targets": _reindex_targets_payload(selected)}


@router.post("/operations/generations/{generation_id}/promote", response_model=GenerationTransition)
async def promote_generation(
    generation_id: UUID,
    payload: GenerationPromoteRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "operations.reindex.mutate")
    _require_mutation_tools(await _active_policy(principal))
    await _require_reindex_operator(session, principal)
    if payload.force and (payload.reason is None or not payload.reason.strip()):
        raise ValidationException("A reason is required when forcing promotion with pending re-embed work.")
    reservation = await _reserve(
        session,
        principal,
        "operations.generation.promote",
        idempotency_key,
        {"generation_id": str(generation_id), "force": payload.force},
    )
    service = EmbeddingGenerationLifecycleService(session)
    if reservation.status is ReservationStatus.REPLAY:
        if not reservation.resource_ids:
            raise ResourceConflictException()
        promoted_id = UUID(reservation.resource_ids[0])
        promoted = await session.get(EmbeddingGenerationModel, promoted_id)
        if promoted is None:
            raise ResourceConflictException()
        previous = reservation.resource_ids[1] if len(reservation.resource_ids) > 1 else None
        return {"id": str(promoted.id), "status": "active", "previous_active_id": previous, "forced": payload.force}
    outcome = await service.promote(generation_id, force=payload.force)
    AuditService(AuditRepository(session)).record(
        actor_member_id=_actor_id(principal),
        actor_kind=principal.kind.value,
        request_id=get_request_id(request),
        action="operations.generation.promoted",
        resource_type="embedding_generation",
        resource_id=str(outcome.promoted.id),
        details={
            "previous_active_id": str(outcome.retired_id) if outcome.retired_id else None,
            "forced": payload.force,
            "reason": payload.reason.strip() if payload.reason else None,
        },
    )
    resource_ids = [str(outcome.promoted.id)]
    if outcome.retired_id is not None:
        resource_ids.append(str(outcome.retired_id))
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=200,
        resource_ids=resource_ids,
    )
    return {
        "id": str(outcome.promoted.id),
        "status": "active",
        "previous_active_id": str(outcome.retired_id) if outcome.retired_id else None,
        "forced": payload.force,
    }


@router.post("/operations/generations/rollback", response_model=GenerationTransition)
async def rollback_generation(
    payload: GenerationRollbackRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "operations.reindex.mutate")
    _require_mutation_tools(await _active_policy(principal))
    await _require_reindex_operator(session, principal)
    reservation = await _reserve(
        session,
        principal,
        "operations.generation.rollback",
        idempotency_key,
        {"reason": payload.reason.strip() if payload.reason else None},
    )
    service = EmbeddingGenerationLifecycleService(session)
    if reservation.status is ReservationStatus.REPLAY:
        if not reservation.resource_ids:
            raise ResourceConflictException()
        activated_id = UUID(reservation.resource_ids[0])
        activated = await session.get(EmbeddingGenerationModel, activated_id)
        if activated is None:
            raise ResourceConflictException()
        previous = reservation.resource_ids[1] if len(reservation.resource_ids) > 1 else None
        return {"id": str(activated.id), "status": "active", "previous_active_id": previous, "forced": False}
    outcome = await service.rollback()
    AuditService(AuditRepository(session)).record(
        actor_member_id=_actor_id(principal),
        actor_kind=principal.kind.value,
        request_id=get_request_id(request),
        action="operations.generation.rolled_back",
        resource_type="embedding_generation",
        resource_id=str(outcome.activated.id),
        details={
            "previous_active_id": str(outcome.retired_id) if outcome.retired_id else None,
            "reason": payload.reason.strip() if payload.reason else None,
        },
    )
    resource_ids = [str(outcome.activated.id)]
    if outcome.retired_id is not None:
        resource_ids.append(str(outcome.retired_id))
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=200,
        resource_ids=resource_ids,
    )
    return {
        "id": str(outcome.activated.id),
        "status": "active",
        "previous_active_id": str(outcome.retired_id) if outcome.retired_id else None,
        "forced": False,
    }


@router.post("/retrieval/search", response_model=RetrievalResult)
async def search_retrieval(
    payload: RetrievalSearchRequest,
    principal: Principal = Depends(require_scope("knowledge:read")),
    idempotency_key: str | None = Header(default=None, min_length=1, max_length=128, alias="Idempotency-Key"),
) -> dict:
    require_profile_route(principal, "knowledge.search")
    policy = await _active_policy(principal)
    search_payload = await RetrievalQueries().search(
        UseCaseContext(principal=principal, policy=policy),
        SearchKnowledgeQuery(
            query=payload.query,
            space_ids=tuple(payload.space_ids) if payload.space_ids is not None else None,
            active_space_id=payload.active_space_id,
            semantic_policy=payload.semantic_policy,
            limit=payload.limit,
            tags=tuple(payload.tags) if payload.tags is not None else None,
        ),
    )
    return search_payload


class EvidenceResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_uri: str = Field(min_length=1, max_length=1024)


@router.post("/evidence/resolve", response_model=EvidenceDetail)
async def resolve_evidence(
    payload: EvidenceResolveRequest,
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.read")
    resolved = await EvidenceService(session).resolve(
        UseCaseContext(principal=principal),
        payload.citation_uri,
    )
    return evidence_response(resolved)


@router.get(
    "/evidence/knowledge/{item_id}/revisions/{revision_id}",
    response_model=EvidenceDetail,
)
async def fetch_knowledge_evidence(
    item_id: UUID,
    revision_id: UUID,
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.read")
    space_id = await _evidence_space(session, "knowledge", item_id)
    fetched = await EvidenceService(session).fetch(
        UseCaseContext(principal=principal),
        EvidenceReference("knowledge_revision", space_id, item_id, revision_id),
    )
    return evidence_response(fetched)


@router.get(
    "/evidence/documents/{document_id}/revisions/{revision_id}",
    response_model=EvidenceDetail,
)
async def fetch_document_evidence(
    document_id: UUID,
    revision_id: UUID,
    chunk_id: UUID | None = Query(default=None),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "knowledge.read")
    space_id = await _evidence_space(session, "document", document_id)
    fetched = await EvidenceService(session).fetch(
        UseCaseContext(principal=principal),
        EvidenceReference("document_chunk", space_id, document_id, revision_id, chunk_id),
    )
    return evidence_response(fetched)


async def _evidence_space(session: AsyncSession, kind: str, canonical_id: UUID) -> str:
    if kind == "knowledge":
        item = await session.get(KnowledgeItemModel, canonical_id)
        if item is None:
            raise AuthorizationException()
        return item.workspace_id
    document = await session.get(DocumentModel, canonical_id)
    if document is None:
        raise AuthorizationException()
    return document.space_id


class ContextAssembleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    space_ids: list[str] | None = Field(default=None, max_length=100)
    active_space_id: str | None = Field(default=None, max_length=64)
    semantic_policy: Literal["prefer", "required", "disabled"] = "prefer"
    max_sources: int = Field(default=8, ge=1, le=25)
    max_snippet_chars: int = Field(default=600, ge=100, le=4000)
    max_total_chars: int = Field(default=8000, ge=500, le=60000)
    tags: list[KnowledgeTag] | None = Field(default=None, max_length=32)


@router.post("/context/assemble", response_model=ContextPackageDetail)
async def assemble_context(
    payload: ContextAssembleRequest,
    principal: Principal = Depends(require_scope("knowledge:read")),
) -> dict:
    require_profile_route(principal, "knowledge.search")
    policy = await _active_policy(principal)
    package = await ContextAssembler().assemble(
        UseCaseContext(principal=principal, policy=policy),
        AssembleContextQuery(
            query=payload.query,
            space_ids=tuple(payload.space_ids) if payload.space_ids is not None else None,
            active_space_id=payload.active_space_id,
            semantic_policy=payload.semantic_policy,
            max_sources=payload.max_sources,
            max_snippet_chars=payload.max_snippet_chars,
            max_total_chars=payload.max_total_chars,
            tags=tuple(payload.tags) if payload.tags is not None else None,
        ),
    )
    return context_package_response(package)


@router.post("/sources/upload", status_code=status.HTTP_202_ACCEPTED, response_model=SourceUploadReceipt)
async def upload_source(
    file: UploadFile = File(...),
    space_id: str = Form(min_length=1, max_length=64),
    display_name: str | None = Form(default=None, max_length=500),
    idempotency_key: str = Header(min_length=1, max_length=255, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    request: Request = None,
) -> dict:
    require_profile_route(principal, "source.upload")
    _require_mutation_tools(await _active_policy(principal))
    filename = file.filename or "uploaded-source"
    gateway = get_settings().gateway
    receipt = await DocumentUploadService(
        get_session_factory(),
        LocalVersionedObjectStorage(gateway.storage_dir),
        max_upload_bytes=gateway.max_upload_bytes,
    ).upload_new(
        principal=principal,
        space_id=space_id,
        display_name=(display_name or filename).strip(),
        original_filename=filename,
        mime_type=file.content_type or "application/octet-stream",
        chunks=_upload_chunks(file),
        idempotency_key=idempotency_key,
    )
    await _record_upload_storage(principal, space_id, receipt.revision_id)
    return {
        "document_id": str(receipt.document_id),
        "revision_id": str(receipt.revision_id),
        "job_id": str(receipt.job_id),
        "job_state": receipt.job_state,
        "duplicate_candidate_revision_id": str(receipt.duplicate_candidate_revision_id) if receipt.duplicate_candidate_revision_id else None,
    }


@router.get("/sources", response_model=Page[SourceSummary])
async def list_sources(
    space_id: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    q: str | None = Query(default=None, min_length=1, max_length=500),
    status_filter: Literal["pending", "processing", "ready", "active", "failed", "quarantined", "cancelled"] | None = Query(
        default=None,
        alias="status",
    ),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "source.list")
    effective_space_ids = await AuthorizationService(session).effective_space_ids(_actor_id(principal), principal=principal)
    if space_id is not None:
        if space_id not in effective_space_ids:
            return {"items": [], **_page_metadata(page=page, page_size=page_size, total_items=0)}
        scoped_spaces: tuple[str, ...] = (space_id,)
    else:
        scoped_spaces = effective_space_ids
        if not scoped_spaces:
            return {"items": [], **_page_metadata(page=page, page_size=page_size, total_items=0)}
    query = (
        select(DocumentModel)
        .outerjoin(DocumentRevisionModel, DocumentRevisionModel.id == DocumentModel.current_revision_id)
        .where(
            DocumentModel.archived_at.is_(None),
            DocumentModel.space_id.in_(scoped_spaces),
        )
        .order_by(DocumentModel.created_at.desc(), DocumentModel.id.desc())
    )
    if q is not None and q.strip():
        pattern = _contains_pattern(q)
        query = query.where(
            or_(
                DocumentModel.display_name.ilike(pattern, escape="\\"),
                DocumentRevisionModel.original_filename.ilike(pattern, escape="\\"),
            )
        )
    if status_filter is not None:
        latest_status = (
            select(DocumentRevisionModel.status)
            .where(DocumentRevisionModel.document_id == DocumentModel.id)
            .order_by(DocumentRevisionModel.version.desc())
            .limit(1)
            .correlate(DocumentModel)
            .scalar_subquery()
        )
        if status_filter == "pending":
            query = query.where(or_(latest_status == status_filter, latest_status.is_(None)))
        else:
            query = query.where(latest_status == status_filter)
    documents, metadata = await _paginate(
        session,
        query,
        page=page,
        page_size=page_size,
        scalars=True,
    )
    document_ids = [document.id for document in documents]
    revisions = list(
        await session.scalars(
            select(DocumentRevisionModel)
            .where(DocumentRevisionModel.document_id.in_(document_ids))
            .order_by(DocumentRevisionModel.document_id, DocumentRevisionModel.version.desc())
        )
    ) if document_ids else []
    latest: dict[UUID, DocumentRevisionModel] = {}
    for revision in revisions:
        latest.setdefault(revision.document_id, revision)
    return {
        "items": [
            {
                "id": str(document.id),
                "space_id": document.space_id,
                "display_name": document.display_name,
                "revision": document.revision,
                "status": latest[document.id].status if document.id in latest else "pending",
                "original_filename": latest[document.id].original_filename if document.id in latest else None,
                "mime_type": latest[document.id].mime_type if document.id in latest else None,
                "size_bytes": latest[document.id].size_bytes if document.id in latest else None,
                "created_at": document.created_at.isoformat(),
                "updated_at": document.updated_at.isoformat(),
            }
            for document in documents
        ],
        **metadata,
    }


@router.delete("/sources/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_source(
    document_id: UUID,
    request: Request,
    expected_revision: int = Query(ge=1),
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    require_profile_route(principal, "source.upload")
    _require_mutation_tools(await _active_policy(principal))
    reservation = await _reserve(
        session,
        principal,
        "source.archive",
        idempotency_key,
        {"document_id": str(document_id), "expected_revision": expected_revision},
    )
    if reservation.status is ReservationStatus.REPLAY:
        return Response(status_code=204)
    document = await session.scalar(
        select(DocumentModel).where(DocumentModel.id == document_id).with_for_update()
    )
    if document is None or document.archived_at is not None:
        raise AuthorizationException()
    await AuthorizationService(session).authorize_space(principal, document.space_id, Action.CONTENT_WRITE)
    if document.revision != expected_revision:
        raise ResourceConflictException("The source changed before it could be archived.")
    current_time = datetime.now(timezone.utc)
    document.archived_at = current_time
    document.revision += 1
    document.updated_at = current_time
    chunk_ids = select(DocumentRevisionChunkModel.id).where(
        DocumentRevisionChunkModel.document_id == document_id
    )
    await session.execute(
        update(RetrievalUnitModel)
        .where(
            RetrievalUnitModel.document_revision_chunk_id.in_(chunk_ids),
            RetrievalUnitModel.active.is_(True),
        )
        .values(active=False, deactivated_at=current_time)
        .execution_options(synchronize_session=False)
    )
    AuditService(AuditRepository(session)).record(
        actor_member_id=_actor_id(principal),
        actor_kind=principal.kind.value,
        request_id=get_request_id(request),
        action="source.archived",
        resource_type="document",
        resource_id=str(document_id),
        details={"space_id": document.space_id, "revision": expected_revision},
    )
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=204,
        resource_ids=[str(document_id)],
    )
    return Response(status_code=204)


class SourceConnectorRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(min_length=1, max_length=64)
    repo: str = Field(min_length=1, max_length=2000)
    branch: str = Field(min_length=1, max_length=255)


def _connector_service() -> GitConnectorService:
    gateway = get_settings().gateway
    return GitConnectorService(
        get_session_factory(),
        LocalVersionedObjectStorage(gateway.storage_dir),
        max_upload_bytes=gateway.max_upload_bytes,
    )


@router.post("/sources/connectors", status_code=status.HTTP_201_CREATED, response_model=SourceConnectorSummary)
async def register_source_connector(
    payload: SourceConnectorRegisterRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "source.upload")
    _require_mutation_tools(await _active_policy(principal))
    reservation = await _reserve(
        session,
        principal,
        "source.connector.register",
        idempotency_key,
        payload.model_dump(mode="json"),
    )
    if reservation.status is ReservationStatus.REPLAY:
        registration = await _connector_service().registration_replay(
            principal=principal,
            space_id=payload.space_id,
            resource_ids=reservation.resource_ids,
        )
    else:
        registration = await _connector_service().register(
            principal=principal,
            space_id=payload.space_id,
            repo=payload.repo,
            branch=payload.branch,
            idempotency_key=idempotency_key,
        )
        AuditService(AuditRepository(session)).record(
            actor_member_id=_actor_id(principal),
            actor_kind=principal.kind.value,
            request_id=get_request_id(request),
            action="connector.registered",
            resource_type="source_connector",
            resource_id=str(registration.connector_id),
            details={"space_id": payload.space_id, "branch": payload.branch.strip()},
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=201,
            resource_ids=[str(registration.connector_id)],
        )
    summaries = await _connector_service().list(principal=principal, space_id=payload.space_id)
    for summary in summaries:
        if summary.id == registration.connector_id:
            return connector_payload(summary)
    raise ResourceConflictException("The connector registration is unavailable.")


@router.get("/sources/connectors", response_model=Page[SourceConnectorSummary])
async def list_source_connectors(
    space_id: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_scope("knowledge:read")),
) -> dict:
    require_profile_route(principal, "source.list")
    summaries = await _connector_service().list(principal=principal, space_id=space_id)
    total_items = len(summaries)
    metadata = _page_metadata(page=page, page_size=page_size, total_items=total_items)
    if metadata["total_pages"] == 0:
        return {"items": [], **metadata}
    start = (metadata["page"] - 1) * page_size
    selected = summaries[start:start + page_size]
    return {"items": [connector_payload(summary) for summary in selected], **metadata}


@router.delete("/sources/connectors/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unregister_source_connector(
    connector_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    require_profile_route(principal, "source.upload")
    _require_mutation_tools(await _active_policy(principal))
    reservation = await _reserve(
        session,
        principal,
        "source.connector.unregister",
        idempotency_key,
        {"connector_id": str(connector_id)},
    )
    if reservation.status is ReservationStatus.REPLAY:
        return Response(status_code=204)
    await _connector_service().unregister(
        principal=principal, connector_id=connector_id, request_id=get_request_id(request)
    )
    AuditService(AuditRepository(session)).record(
        actor_member_id=_actor_id(principal),
        actor_kind=principal.kind.value,
        request_id=get_request_id(request),
        action="connector.unregistered",
        resource_type="source_connector",
        resource_id=str(connector_id),
        details={},
    )
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=204,
        resource_ids=[str(connector_id)],
    )
    return Response(status_code=204)


@router.post("/sources/connectors/{connector_id}/sync", status_code=status.HTTP_202_ACCEPTED, response_model=SourceConnectorSyncReceipt)
async def sync_source_connector(
    connector_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "source.upload")
    _require_mutation_tools(await _active_policy(principal))
    reservation = await _reserve(
        session,
        principal,
        "source.connector.sync",
        idempotency_key,
        {"connector_id": str(connector_id)},
    )
    if reservation.status is ReservationStatus.REPLAY:
        if len(reservation.resource_ids) >= 6:
            return {
                "connector_id": reservation.resource_ids[0],
                "commit": reservation.resource_ids[1],
                "enqueued": int(reservation.resource_ids[2]),
                "archived": int(reservation.resource_ids[3]),
                "skipped": int(reservation.resource_ids[4]),
                "skipped_reasons": reservation.resource_ids[5].split("\n") if reservation.resource_ids[5] else [],
            }
        raise ResourceConflictException("The connector sync result is unavailable.")
    result = await _connector_service().sync(
        principal=principal,
        connector_id=connector_id,
        idempotency_key=idempotency_key,
    )
    AuditService(AuditRepository(session)).record(
        actor_member_id=_actor_id(principal),
        actor_kind=principal.kind.value,
        request_id=get_request_id(request),
        action="connector.synced",
        resource_type="source_connector",
        resource_id=str(connector_id),
        details={"commit": result.commit, "enqueued": result.enqueued, "archived": result.archived},
    )
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=202,
        resource_ids=[
            str(result.connector_id),
            result.commit,
            str(result.enqueued),
            str(result.archived),
            str(result.skipped),
            "\n".join(result.skipped_reasons[:32]),
        ],
    )
    return sync_result_payload(result)


@router.get("/ingestion-jobs", response_model=Page[IngestionJob])
async def list_ingestion_jobs(
    space_id: str | None = Query(default=None, max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    state: Literal["preparing", "queued", "running", "retry_wait", "succeeded", "failed", "cancelled"] | None = Query(default=None),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "ingestion_job.list")
    effective_space_ids = await AuthorizationService(session).effective_space_ids(_actor_id(principal), principal=principal)
    if space_id is not None:
        if space_id not in effective_space_ids:
            return {"items": [], **_page_metadata(page=page, page_size=page_size, total_items=0)}
        scoped_spaces: tuple[str, ...] = (space_id,)
    else:
        scoped_spaces = effective_space_ids
        if not scoped_spaces:
            return {"items": [], **_page_metadata(page=page, page_size=page_size, total_items=0)}
    query = (
        select(IngestionJobModel)
        .where(IngestionJobModel.space_id.in_(scoped_spaces))
        .order_by(IngestionJobModel.created_at.desc(), IngestionJobModel.id.desc())
    )
    if state is not None:
        query = query.where(IngestionJobModel.state == state)
    jobs, metadata = await _paginate(
        session,
        query,
        page=page,
        page_size=page_size,
        scalars=True,
    )
    return {
        "items": [
            {
                "id": str(job.id),
                "space_id": job.space_id,
                "job_type": job.job_type,
                "document_id": str(job.document_id) if job.document_id is not None else None,
                "document_revision_id": str(job.document_revision_id) if job.document_revision_id is not None else None,
                "knowledge_item_id": str(job.knowledge_item_id) if job.knowledge_item_id is not None else None,
                "knowledge_revision_id": str(job.knowledge_revision_id) if job.knowledge_revision_id is not None else None,
                "state": job.state,
                "progress": job.progress,
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
                "last_error_code": job.last_error_code,
                "created_at": job.created_at.isoformat(),
                "updated_at": job.updated_at.isoformat(),
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            }
            for job in jobs
        ],
        **metadata,
    }


async def _mutate_ingestion_job(
    job_id: UUID,
    operation: str,
    request: Request,
    idempotency_key: str,
    principal: Principal,
    session: AsyncSession,
) -> dict:
    _require_mutation_tools(await _active_policy(principal))
    job = await session.get(IngestionJobModel, job_id)
    if job is None:
        raise AuthorizationException()
    await AuthorizationService(session).authorize_space(principal, job.space_id, Action.CONTENT_WRITE)
    reservation = await _reserve(
        session,
        principal,
        f"ingestion_job.{operation}",
        idempotency_key,
        {"job_id": str(job_id)},
    )
    if reservation.status is ReservationStatus.REPLAY:
        if operation == "cancel" and job.cancellation_requested and job.state not in {"failed", "cancelled", "succeeded"}:
            state = "cancellation_requested"
        elif operation == "retry" and job.retry_requested:
            state = "retry_requested"
        else:
            state = job.state
    elif operation == "cancel":
        state = await IngestionJobService(session).request_cancellation(job_id)
    else:
        state = await IngestionJobService(session).request_retry(job_id)
    if reservation.status is not ReservationStatus.REPLAY:
        AuditService(AuditRepository(session)).record(
            actor_member_id=_actor_id(principal),
            actor_kind=principal.kind.value,
            request_id=get_request_id(request),
            action=f"ingestion_job.{operation}_requested",
            resource_type="ingestion_job",
            resource_id=str(job_id),
            details={"space_id": job.space_id, "state": state},
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=[str(job_id)],
        )
    return {"id": str(job_id), "state": state}


@router.post("/ingestion-jobs/{job_id}/cancel", response_model=IngestionMutation)
async def cancel_ingestion_job(
    job_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "ingestion_job.mutate")
    return await _mutate_ingestion_job(job_id, "cancel", request, idempotency_key, principal, session)


@router.post("/ingestion-jobs/{job_id}/retry", response_model=IngestionMutation)
async def retry_ingestion_job(
    job_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "ingestion_job.mutate")
    return await _mutate_ingestion_job(job_id, "retry", request, idempotency_key, principal, session)


@router.get("/events", response_model=Page[WebhookEventDetail])
async def list_webhook_events(
    event_type: str | None = Query(default=None, min_length=1, max_length=64),
    after_id: UUID | None = Query(default=None),
    space_id: str | None = Query(default=None, min_length=1, max_length=64),
    page_size: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "webhook.read")
    from src.gateway.application.services.webhook_service import list_webhook_events as _list_events

    effective = await AuthorizationService(session).effective_space_ids(_actor_id(principal), principal=principal)
    if not effective:
        return {"items": [], **_page_metadata(page=1, page_size=page_size, total_items=0)}
    if space_id is not None:
        if space_id not in effective:
            return {"items": [], **_page_metadata(page=1, page_size=page_size, total_items=0)}
        scoped = (space_id,)
    else:
        scoped = effective
    rows = await _list_events(session, event_type=event_type, after_id=after_id, limit=page_size, space_ids=scoped)
    return {
        "items": [
            {
                "id": str(event.id),
                "event_type": event.event_type,
                "job_id": str(event.job_id),
                "deduplication_key": event.deduplication_key,
                "payload": dict(event.payload or {}),
                "created_at": event.created_at,
            }
            for event in rows
        ],
        **_page_metadata(page=1, page_size=page_size, total_items=len(rows)),
    }


@router.get("/api-keys", response_model=Page[APIKeySummary])
async def list_api_keys(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    q: str | None = Query(default=None, min_length=1, max_length=255),
    status_filter: Literal["active", "revoked", "expired"] | None = Query(default=None, alias="status"),
    principal: Principal = Depends(require_scope("api_keys:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "api_key.manage")
    member_id = _actor_id(principal)
    query = (
        select(PersonalAPIKeyModel)
        .where(PersonalAPIKeyModel.member_id == member_id)
        .order_by(PersonalAPIKeyModel.created_at.desc(), PersonalAPIKeyModel.id.desc())
    )
    if q is not None and q.strip():
        pattern = _contains_pattern(q)
        query = query.where(
            or_(
                PersonalAPIKeyModel.name.ilike(pattern, escape="\\"),
                PersonalAPIKeyModel.public_id.ilike(pattern, escape="\\"),
            )
        )
    if status_filter is not None:
        query = query.where(PersonalAPIKeyModel.status == status_filter)
    keys, metadata = await _paginate(
        session,
        query,
        page=page,
        page_size=page_size,
        scalars=True,
    )
    key_ids = [key.id for key in keys]
    scope_rows = (
        (
            await session.execute(
                select(APIKeyScopeModel.api_key_id, APIKeyScopeModel.scope).where(
                    APIKeyScopeModel.api_key_id.in_(key_ids)
                )
            )
        ).all()
        if key_ids
        else []
    )
    grant_rows = (
        (
            await session.execute(
                select(APIKeySpaceGrantModel.api_key_id, APIKeySpaceGrantModel.space_id).where(
                    APIKeySpaceGrantModel.api_key_id.in_(key_ids)
                )
            )
        ).all()
        if key_ids
        else []
    )
    scopes: dict[UUID, list[str]] = {key_id: [] for key_id in key_ids}
    for key_id, scope in scope_rows:
        scopes[key_id].append(scope)
    grant_map: dict[UUID, list[str]] = {key_id: [] for key_id in key_ids}
    for key_id, space_id in grant_rows:
        grant_map[key_id].append(space_id)
    return {
        "items": [
            {
                "id": str(key.id),
                "public_id": key.public_id,
                "name": key.name,
                "status": key.status,
                "scopes": sorted(scopes[key.id]),
                "space_grants": sorted(grant_map[key.id]) if grant_map[key.id] else None,
                "created_at": key.created_at.isoformat(),
                "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
                "expires_at": key.expires_at.isoformat() if key.expires_at else None,
                "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
            }
            for key in keys
        ],
        **metadata,
    }


@router.post("/api-keys", status_code=status.HTTP_201_CREATED, response_model=CreatedAPIKey)
async def create_api_key(
    payload: APIKeyCreateRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("api_keys:write")),
    session: AsyncSession = Depends(get_db_session),
) -> CreatedAPIKey:
    require_profile_route(principal, "api_key.manage")
    reservation = await _reserve(
        session,
        principal,
        "api_key.create",
        idempotency_key,
        payload.model_dump(mode="json"),
    )
    if reservation.status is ReservationStatus.REPLAY:
        raise ResourceConflictException("The API key secret was already revealed and cannot be replayed.")
    try:
        created = await APIKeyService(session, configured_api_key_codec()).create(
            _actor_id(principal),
            family_id=await _family_id(session, principal),
            name=payload.name,
            scopes=set(payload.scopes),
            expires_at=payload.expires_at,
            space_grants=set(payload.space_grants) if payload.space_grants is not None else None,
            permission_profile=PermissionProfile(payload.permission_profile) if payload.permission_profile else None,
            request_id=get_request_id(request),
        )
    except ValueError as exc:
        raise ValidationException(str(exc)) from exc
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=201,
        resource_ids=[str(created.key_id)],
    )
    response.headers["Cache-Control"] = "no-store"
    return CreatedAPIKey(
        id=str(created.key_id),
        public_id=created.public_id,
        secret=created.secret.reveal(),
        scopes=sorted(created.scopes),
        expires_at=created.expires_at,
        space_grants=sorted(created.space_grants) if created.space_grants is not None else None,
        permission_profile=created.permission_profile.value if created.permission_profile else None,
    )


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("api_keys:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    require_profile_route(principal, "api_key.manage")
    reservation = await _reserve(
        session,
        principal,
        "api_key.revoke",
        idempotency_key,
        {"key_id": str(key_id)},
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await APIKeyService(session, configured_api_key_codec()).revoke(
            principal,
            key_id,
            request_id=get_request_id(request),
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=204,
            resource_ids=[str(key_id)],
        )
    return Response(status_code=204)


@router.get("/members", response_model=Page[MemberSummary])
async def list_members(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    q: str | None = Query(default=None, min_length=1, max_length=255),
    status_filter: Literal["pending", "active", "disabled"] | None = Query(default=None, alias="status"),
    system_role: Literal["super_admin", "member"] | None = Query(default=None),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "member.admin")
    if principal.system_role is not SystemRole.SUPER_ADMIN:
        raise AuthorizationException()
    mfa_enabled = exists().where(
        MFAFactorModel.member_id == MemberModel.id,
        MFAFactorModel.confirmed_at.is_not(None),
        MFAFactorModel.retired_at.is_(None),
    ).label("mfa_enabled")
    query = select(MemberModel, mfa_enabled).order_by(MemberModel.username_normalized)
    if q is not None and q.strip():
        pattern = _contains_pattern(q)
        query = query.where(
            or_(
                MemberModel.username.ilike(pattern, escape="\\"),
                MemberModel.username_normalized.ilike(pattern, escape="\\"),
                MemberModel.display_name.ilike(pattern, escape="\\"),
            )
        )
    if status_filter is not None:
        query = query.where(MemberModel.status == status_filter)
    if system_role is not None:
        query = query.where(MemberModel.system_role == system_role)
    rows, metadata = await _paginate(
        session,
        query,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [
            {
                "id": str(member.id),
                "username": member.username,
                "display_name": member.display_name,
                "status": member.status,
                "system_role": member.system_role,
                "requires_password_change": member.force_password_change,
                "mfa_enabled": bool(member_mfa_enabled),
                "created_at": member.created_at.isoformat(),
                "updated_at": member.updated_at.isoformat(),
            }
            for member, member_mfa_enabled in rows
        ],
        **metadata,
    }


@router.post("/members", status_code=status.HTTP_201_CREATED, response_model=CreatedMember)
async def create_member(
    payload: MemberCreateRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> CreatedMember:
    require_profile_route(principal, "member.admin")
    reservation = await _reserve(
        session,
        principal,
        "member.create",
        idempotency_key,
        payload.model_dump(mode="json"),
    )
    if reservation.status is ReservationStatus.REPLAY:
        raise ResourceConflictException("The temporary password was already revealed and cannot be replayed.")
    try:
        created = await MemberAdministrationService(session, PasswordService()).create(
            principal,
            family_id=await _family_id(session, principal),
            username=payload.username,
            display_name=payload.display_name,
            request_id=get_request_id(request),
        )
    except ValueError as exc:
        raise ValidationException(str(exc)) from exc
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=201,
        resource_ids=[str(created.member_id)],
    )
    response.headers["Cache-Control"] = "no-store"
    return CreatedMember(
        id=str(created.member_id),
        username=created.username,
        display_name=created.display_name,
        temporary_password=created.temporary_password.reveal(),
        temporary_password_expires_at=created.expires_at,
        requires_password_change=True,
    )


@router.patch("/members/{member_id}", response_model=MemberSummary)
async def update_member(
    member_id: UUID,
    payload: MemberUpdateRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "member.admin")
    reservation = await _reserve(
        session,
        principal,
        "member.update",
        idempotency_key,
        {"member_id": str(member_id), **payload.model_dump(mode="json")},
    )
    if reservation.status is not ReservationStatus.REPLAY:
        try:
            await MemberAdministrationService(session, PasswordService()).update(
                principal,
                family_id=await _family_id(session, principal),
                member_id=member_id,
                display_name=payload.display_name,
                status=payload.status,
                system_role=payload.system_role,
                request_id=get_request_id(request),
            )
        except ValueError as exc:
            raise ValidationException(str(exc)) from exc
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=[str(member_id)],
        )
    member = await session.get(MemberModel, member_id)
    if member is None:
        raise AuthorizationException()
    mfa_enabled = bool(await session.scalar(select(exists().where(
        MFAFactorModel.member_id == member.id,
        MFAFactorModel.confirmed_at.is_not(None),
        MFAFactorModel.retired_at.is_(None),
    ))))
    return {
        "id": str(member.id),
        "username": member.username,
        "display_name": member.display_name,
        "status": member.status,
        "system_role": member.system_role,
        "requires_password_change": member.force_password_change,
        "mfa_enabled": mfa_enabled,
    }


@router.post("/members/{member_id}/password-reset", response_model=ResetMemberPassword)
async def reset_member_password(
    member_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> ResetMemberPassword:
    require_profile_route(principal, "member.admin")
    reservation = await _reserve(
        session,
        principal,
        "member.password_reset",
        idempotency_key,
        {"member_id": str(member_id)},
    )
    if reservation.status is ReservationStatus.REPLAY:
        raise ResourceConflictException("The temporary password was already revealed and cannot be replayed.")
    reset = await MemberAdministrationService(session, PasswordService()).reset_password(
        principal,
        family_id=await _family_id(session, principal),
        member_id=member_id,
        request_id=get_request_id(request),
    )
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=200,
        resource_ids=[str(member_id)],
    )
    response.headers["Cache-Control"] = "no-store"
    return ResetMemberPassword(
        id=str(reset.member_id),
        temporary_password=reset.temporary_password.reveal(),
        temporary_password_expires_at=reset.expires_at,
        requires_password_change=True,
    )


@router.get("/sessions", response_model=Page[SessionSummary])
async def list_sessions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    status_filter: Literal["active", "expired", "revoked"] | None = Query(default=None, alias="status"),
    principal: Principal = Depends(require_scope("sessions:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "session.manage")
    current_family = await _family_id(session, principal)
    current_time = datetime.now(timezone.utc)
    query = (
        select(SessionFamilyModel)
        .where(SessionFamilyModel.member_id == _actor_id(principal))
        .order_by(SessionFamilyModel.created_at.desc(), SessionFamilyModel.id.desc())
    )
    if status_filter == "active":
        query = query.where(
            SessionFamilyModel.revoked_at.is_(None),
            SessionFamilyModel.idle_expires_at > current_time,
            SessionFamilyModel.absolute_expires_at > current_time,
        )
    elif status_filter == "revoked":
        query = query.where(SessionFamilyModel.revoked_at.is_not(None))
    elif status_filter == "expired":
        query = query.where(
            SessionFamilyModel.revoked_at.is_(None),
            or_(
                SessionFamilyModel.idle_expires_at <= current_time,
                SessionFamilyModel.absolute_expires_at <= current_time,
            ),
        )
    rows, metadata = await _paginate(
        session,
        query,
        page=page,
        page_size=page_size,
        scalars=True,
    )
    return {
        "items": [
            {
                "id": str(family.id),
                "current": family.id == current_family,
                "status": "revoked" if family.revoked_at else ("expired" if current_time >= family.absolute_expires_at or current_time >= family.idle_expires_at else "active"),
                "created_at": family.created_at.isoformat(),
                "last_activity_at": family.last_activity_at.isoformat(),
                "idle_expires_at": family.idle_expires_at.isoformat(),
                "absolute_expires_at": family.absolute_expires_at.isoformat(),
                "revoked_at": family.revoked_at.isoformat() if family.revoked_at else None,
            }
            for family in rows
        ],
        **metadata,
    }


@router.delete("/sessions/{family_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    family_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("sessions:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    require_profile_route(principal, "session.manage")
    family = await session.get(SessionFamilyModel, family_id)
    if family is None or family.member_id != _actor_id(principal):
        raise AuthorizationException()
    reservation = await _reserve(
        session,
        principal,
        "session.revoke",
        idempotency_key,
        {"family_id": str(family_id)},
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await SessionService(session).revoke_family(family_id, reason="member_revoked")
        AuditService(AuditRepository(session)).record(
            actor_member_id=_actor_id(principal),
            actor_kind=principal.kind.value,
            request_id=get_request_id(request),
            action="session.revoked",
            resource_type="session_family",
            resource_id=str(family_id),
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=204,
            resource_ids=[str(family_id)],
        )
    return Response(status_code=204)


@router.get("/audit-events", response_model=Page[AuditEvent])
async def list_audit_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    q: str | None = Query(default=None, min_length=1, max_length=255),
    action_filter: str | None = Query(default=None, min_length=1, max_length=128, alias="action"),
    outcome: Literal["success", "denied", "failed"] | None = Query(default=None),
    resource_type: str | None = Query(default=None, min_length=1, max_length=64),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    require_profile_route(principal, "member.admin")
    if principal.system_role is not SystemRole.SUPER_ADMIN:
        raise AuthorizationException()
    query = select(AuditEventModel).order_by(AuditEventModel.occurred_at.desc(), AuditEventModel.id.desc())
    if q is not None and q.strip():
        pattern = _contains_pattern(q)
        query = query.where(
            or_(
                AuditEventModel.action.ilike(pattern, escape="\\"),
                AuditEventModel.resource_type.ilike(pattern, escape="\\"),
                AuditEventModel.resource_id.ilike(pattern, escape="\\"),
                AuditEventModel.request_id.ilike(pattern, escape="\\"),
            )
        )
    if action_filter is not None:
        query = query.where(AuditEventModel.action == action_filter)
    if outcome is not None:
        query = query.where(AuditEventModel.outcome == outcome)
    if resource_type is not None:
        query = query.where(AuditEventModel.resource_type == resource_type)
    rows, metadata = await _paginate(
        session,
        query,
        page=page,
        page_size=page_size,
        scalars=True,
    )
    return {
        "items": [
            {
                "id": str(event.id),
                "occurred_at": event.occurred_at.isoformat(),
                "actor_member_id": str(event.actor_member_id) if event.actor_member_id else None,
                "actor_kind": event.actor_kind,
                "action": event.action,
                "resource_type": event.resource_type,
                "resource_id": event.resource_id,
                "outcome": event.outcome,
                "request_id": event.request_id,
            }
            for event in rows
        ],
        **metadata,
    }


def _audit_event_filters(
    q: str | None,
    action_filter: str | None,
    outcome: Literal["success", "denied", "failed"] | None,
    resource_type: str | None,
):
    query = select(AuditEventModel).order_by(AuditEventModel.occurred_at.desc(), AuditEventModel.id.desc())
    if q is not None and q.strip():
        pattern = _contains_pattern(q)
        query = query.where(
            or_(
                AuditEventModel.action.ilike(pattern, escape="\\"),
                AuditEventModel.resource_type.ilike(pattern, escape="\\"),
                AuditEventModel.resource_id.ilike(pattern, escape="\\"),
                AuditEventModel.request_id.ilike(pattern, escape="\\"),
            )
        )
    if action_filter is not None:
        query = query.where(AuditEventModel.action == action_filter)
    if outcome is not None:
        query = query.where(AuditEventModel.outcome == outcome)
    if resource_type is not None:
        query = query.where(AuditEventModel.resource_type == resource_type)
    return query


@router.get("/audit-events/export")
async def export_audit_events(
    q: str | None = Query(default=None, min_length=1, max_length=255),
    action_filter: str | None = Query(default=None, min_length=1, max_length=128, alias="action"),
    outcome: Literal["success", "denied", "failed"] | None = Query(default=None),
    resource_type: str | None = Query(default=None, min_length=1, max_length=64),
    format: Literal["csv", "jsonl"] = Query(default="csv"),
    limit: int = Query(default=10000, ge=1, le=50000),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
):
    require_profile_route(principal, "member.admin")
    if principal.system_role is not SystemRole.SUPER_ADMIN:
        raise AuthorizationException()
    query = _audit_event_filters(q, action_filter, outcome, resource_type).limit(limit)
    rows = list(await session.scalars(query))
    if format == "jsonl":
        lines = [
            _audit_event_json(event).encode("utf-8") + b"\n"
            for event in rows
        ]
        return StreamingResponse(
            _iter_bytes(lines),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=audit-events.jsonl"},
        )
    header = b"id,occurred_at,actor_member_id,actor_kind,action,resource_type,resource_id,outcome,request_id\n"
    lines = [header] + [_audit_event_csv(event) for event in rows]
    return StreamingResponse(
        _iter_bytes(lines),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-events.csv"},
    )


def _audit_event_json(event: AuditEventModel) -> str:
    return json.dumps(
        {
            "id": str(event.id),
            "occurred_at": event.occurred_at.isoformat(),
            "actor_member_id": str(event.actor_member_id) if event.actor_member_id else None,
            "actor_kind": event.actor_kind,
            "action": event.action,
            "resource_type": event.resource_type,
            "resource_id": event.resource_id,
            "outcome": event.outcome,
            "request_id": event.request_id,
        },
        ensure_ascii=False,
    )


def _audit_event_csv(event: AuditEventModel) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer).writerow(
        [
            str(event.id),
            event.occurred_at.isoformat(),
            str(event.actor_member_id) if event.actor_member_id else "",
            event.actor_kind,
            event.action,
            event.resource_type,
            event.resource_id or "",
            event.outcome,
            event.request_id,
        ]
    )
    return buffer.getvalue().encode("utf-8")


async def _iter_bytes(chunks):
    for chunk in chunks:
        yield chunk


@router.get("/settings", response_model=RuntimeSettings)
async def active_runtime_settings(
    principal: Principal = Depends(require_scope("settings:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    return _settings_payload(await RuntimeSettingsService(session).active())


@router.get("/settings/history", response_model=Page[RuntimeSettings])
async def runtime_settings_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    state: Literal["active", "superseded"] | None = Query(default=None),
    principal: Principal = Depends(require_scope("settings:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    query = (
        select(RuntimeSettingRevisionModel)
        .where(RuntimeSettingRevisionModel.state.in_(("active", "superseded")))
        .order_by(RuntimeSettingRevisionModel.revision.desc())
    )
    if state is not None:
        query = query.where(RuntimeSettingRevisionModel.state == state)
    history, metadata = await _paginate(
        session,
        query,
        page=page,
        page_size=page_size,
        scalars=True,
    )
    return {
        "items": [_settings_payload(revision) for revision in history],
        **metadata,
    }


@router.post("/settings/drafts", status_code=status.HTTP_201_CREATED, response_model=RuntimeSettings)
async def create_runtime_settings_draft(
    payload: RuntimeSettingsDraftRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("settings:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    reservation = await _reserve(
        session,
        principal,
        "runtime_settings.draft",
        idempotency_key,
        payload.model_dump(mode="json"),
    )
    if reservation.status is ReservationStatus.REPLAY:
        if not reservation.resource_ids:
            raise ResourceConflictException()
        model = await session.get(RuntimeSettingRevisionModel, UUID(reservation.resource_ids[0]))
        if model is None:
            raise ResourceConflictException()
        return _settings_payload(RuntimeSettingsService._to_domain(model))
    try:
        draft = await RuntimeSettingsService(session).create_draft(
            principal,
            family_id=await _family_id(session, principal),
            base_revision=payload.base_revision,
            values=payload.values,
            reason=payload.reason,
            request_id=get_request_id(request),
        )
    except ValueError as exc:
        raise ValidationException(str(exc)) from exc
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=201,
        resource_ids=[str(draft.id)],
    )
    return _settings_payload(draft)


@router.post("/settings/drafts/{draft_id}/activate", response_model=RuntimeSettings)
async def activate_runtime_settings_draft(
    draft_id: UUID,
    payload: RuntimeSettingsActivationRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("settings:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    reservation = await _reserve(
        session,
        principal,
        "runtime_settings.activate",
        idempotency_key,
        {"draft_id": str(draft_id), **payload.model_dump(mode="json")},
    )
    if reservation.status is ReservationStatus.REPLAY:
        if not reservation.resource_ids:
            raise ResourceConflictException()
        model = await session.get(RuntimeSettingRevisionModel, UUID(reservation.resource_ids[0]))
        if model is None:
            raise ResourceConflictException()
        return _settings_payload(RuntimeSettingsService._to_domain(model))
    try:
        activated = await RuntimeSettingsService(
            session,
            dependency_probe=_runtime_settings_dependency_probe,
        ).activate(
            principal,
            family_id=await _family_id(session, principal),
            draft_id=draft_id,
            expected_active_revision=payload.expected_active_revision,
            reason=payload.reason,
            request_id=get_request_id(request),
        )
    except ValueError as exc:
        raise ValidationException(str(exc)) from exc
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=200,
        resource_ids=[str(activated.id)],
    )
    return _settings_payload(activated)


@router.post("/settings/rollback/{target_revision}", response_model=RuntimeSettings)
async def rollback_runtime_settings(
    target_revision: int,
    payload: RuntimeSettingsRollbackRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("settings:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    if target_revision < 1:
        raise ValidationException("A persisted runtime settings revision is required.")
    reservation = await _reserve(
        session,
        principal,
        "runtime_settings.rollback",
        idempotency_key,
        {"target_revision": target_revision, **payload.model_dump(mode="json")},
    )
    if reservation.status is ReservationStatus.REPLAY:
        if not reservation.resource_ids:
            raise ResourceConflictException()
        model = await session.get(RuntimeSettingRevisionModel, UUID(reservation.resource_ids[0]))
        if model is None:
            raise ResourceConflictException()
        return _settings_payload(RuntimeSettingsService._to_domain(model))
    try:
        restored = await RuntimeSettingsService(
            session,
            dependency_probe=_runtime_settings_dependency_probe,
        ).rollback(
            principal,
            family_id=await _family_id(session, principal),
            target_revision=target_revision,
            expected_active_revision=payload.expected_active_revision,
            reason=payload.reason,
            request_id=get_request_id(request),
        )
    except ValueError as exc:
        raise ValidationException(str(exc)) from exc
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=200,
        resource_ids=[str(restored.id)],
    )
    return _settings_payload(restored)
