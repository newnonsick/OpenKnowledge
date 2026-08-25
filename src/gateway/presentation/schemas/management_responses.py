from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from src.gateway.application.services.runtime_settings_service import RuntimeSettingsValues
from src.gateway.domain.identity import MemberStatus, SpaceRole, SystemRole


T = TypeVar("T")
MANAGEMENT_ERROR_STATUSES = (401, 403, 404, 409, 413, 422, 429, 500, 502)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ManagementErrorCode = Literal[
    "csrf_verification_failed",
    "embedding_provider_error",
    "gateway_error",
    "http_error",
    "internal_error",
    "invalid_api_key",
    "invalid_payload",
    "job_lease_lost",
    "llm_service_failed",
    "login_throttled",
    "model_not_found",
    "parser_timeout",
    "recent_authentication_required",
    "resource_conflict",
    "resource_not_found",
    "resource_unavailable",
    "storage_operation_failed",
    "tool_failed",
    "upload_too_large",
    "version_mismatch",
]
DocumentStatus = Literal["pending", "processing", "ready", "active", "failed", "quarantined", "cancelled"]
IngestionJobState = Literal["preparing", "queued", "running", "retry_wait", "succeeded", "failed", "cancelled"]
IngestionMutationState = IngestionJobState | Literal["cancellation_requested", "retry_requested"]
APIKeyState = Literal["active", "revoked", "expired"]
SessionState = Literal["active", "revoked", "expired"]
RuntimeSettingsState = Literal["draft", "active", "superseded"]
PendingActionState = Literal["pending", "executed", "expired", "cancelled"]
RetrievalSemanticState = Literal["active", "degraded", "disabled"]
AuditOutcome = Literal["success", "denied", "failed"]


class ErrorFieldDetail(ContractModel):
    field: str
    code: str


class ErrorDetails(ContractModel):
    fields: list[ErrorFieldDetail] = Field(default_factory=list)
    retry_after_seconds: int | None = None
    item_id: str | None = None
    expected_version: int | None = None
    actual_version: int | None = None


class ErrorDetail(ContractModel):
    message: str
    type: str
    code: ManagementErrorCode
    details: ErrorDetails | None = None


class ErrorEnvelope(ContractModel):
    error: ErrorDetail
    request_id: str


MANAGEMENT_ERROR_RESPONSES = {
    error_status: {"model": ErrorEnvelope}
    for error_status in MANAGEMENT_ERROR_STATUSES
}


class Page(ContractModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total_items: int
    total_pages: int


class IngestionCounts(ContractModel):
    queued: int
    retry_wait: int
    running: int
    cancellation_requested: int
    succeeded: int
    failed: int
    cancelled: int


class StorageSummary(ContractModel):
    referenced_bytes: int


class RetrievalSummary(ContractModel):
    embedding_generation_active: bool


class OperationSummary(ContractModel):
    scope: Literal["accessible_spaces"]
    observed_at: datetime
    spaces: int
    ingestion: IngestionCounts
    storage: StorageSummary
    retrieval: RetrievalSummary
    settings_revision: int


class InitialAPIKey(ContractModel):
    id: str
    public_id: str
    name: str
    secret: str
    scopes: list[str]
    expires_at: datetime | None


class SessionAuthentication(ContractModel):
    member_id: str
    system_role: SystemRole
    requires_password_change: bool
    requires_mfa_enrollment: bool
    access_expires_at: datetime
    initial_api_key: InitialAPIKey | None = None


class SessionRefresh(ContractModel):
    status: Literal["refreshed"]
    access_expires_at: datetime


class SessionStepUp(ContractModel):
    status: Literal["reauthenticated"]
    step_up_expires_at: datetime


class TotpEnrollment(ContractModel):
    factor_id: str
    secret: str


class TotpConfirmation(SessionAuthentication):
    recovery_codes: list[str]


class SignOut(ContractModel):
    status: Literal["signed_out"]


class AIToolDescriptor(ContractModel):
    name: str
    description: str
    confirmation: Literal["none", "required"]
    parameters: dict[str, Any]


class AIToolList(ContractModel):
    items: list[AIToolDescriptor]


class PendingAIAction(ContractModel):
    id: str
    tool_name: str
    target_ids: list[str]
    expected_revision: int | None
    created_at: datetime
    expires_at: datetime
    status: Literal["pending"]


class ConfirmedAIAction(ContractModel):
    pending_action_id: str
    status: Literal["executed"]
    tool_name: str


class CurrentMember(ContractModel):
    id: str
    username: str
    display_name: str
    status: MemberStatus
    system_role: SystemRole
    requires_password_change: bool


class SpaceSummary(ContractModel):
    id: str
    name: str
    role: SpaceRole
    revision: int
    personal: bool
    created_at: datetime


class AdminSpaceSummary(ContractModel):
    id: str
    name: str
    revision: int
    owner_member_id: str
    owner_username: str
    owner_display_name: str
    created_at: datetime


class CreatedSpace(ContractModel):
    id: str
    name: str
    role: SpaceRole
    revision: int


class SpaceMember(ContractModel):
    member_id: str
    username: str
    display_name: str
    status: MemberStatus
    role: SpaceRole
    updated_at: datetime


class SpaceMemberCandidate(ContractModel):
    member_id: str
    username: str
    display_name: str


class SpaceMembership(ContractModel):
    space_id: str
    member_id: str
    role: SpaceRole


class KnowledgeSummary(ContractModel):
    id: str
    space_id: str
    title: str
    content_excerpt: str
    tags: list[str]
    version: int
    created_at: datetime
    updated_at: datetime


class KnowledgeDetail(KnowledgeSummary):
    content: str


class RetrievalHit(ContractModel):
    rank: int
    rank_score: float
    source_type: str
    space_id: str
    canonical_id: str
    revision_id: str
    title: str
    content_excerpt: str
    citation_uri: str | None
    language: str | None
    source_filename: str | None
    version: int | None


class RetrievalHealth(ContractModel):
    semantic_status: RetrievalSemanticState
    degraded_reasons: list[str]
    embedding_generation_id: str | None
    embedding_coverage: float | None


class RetrievalExplanation(ContractModel):
    effective_space_ids: list[str]
    abstained: bool
    active_space_id: str | None


class RetrievalResult(ContractModel):
    query: str
    hits: list[RetrievalHit]
    health: RetrievalHealth
    explanation: RetrievalExplanation


class SourceUploadReceipt(ContractModel):
    document_id: str
    revision_id: str
    job_id: str
    job_state: Literal["preparing", "queued"]
    duplicate_candidate_revision_id: str | None


class SourceSummary(ContractModel):
    id: str
    space_id: str
    display_name: str
    revision: int
    status: DocumentStatus
    original_filename: str | None
    mime_type: str | None
    size_bytes: int | None
    created_at: datetime
    updated_at: datetime


class IngestionJob(ContractModel):
    id: str
    space_id: str
    document_id: str
    document_revision_id: str
    state: IngestionJobState
    progress: int
    attempt_count: int
    max_attempts: int
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class IngestionMutation(ContractModel):
    id: str
    state: IngestionMutationState


class APIKeySummary(ContractModel):
    id: str
    public_id: str
    name: str
    status: APIKeyState
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class CreatedAPIKey(ContractModel):
    id: str
    public_id: str
    secret: str
    scopes: list[str]
    expires_at: datetime | None


class MemberSummary(CurrentMember):
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CreatedMember(ContractModel):
    id: str
    username: str
    display_name: str
    temporary_password: str
    temporary_password_expires_at: datetime
    requires_password_change: Literal[True]


class ResetMemberPassword(ContractModel):
    id: str
    temporary_password: str
    temporary_password_expires_at: datetime
    requires_password_change: Literal[True]


class SessionSummary(ContractModel):
    id: str
    current: bool
    status: SessionState
    created_at: datetime
    last_activity_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    revoked_at: datetime | None


class AuditEvent(ContractModel):
    id: str
    occurred_at: datetime
    actor_member_id: str | None
    actor_kind: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcome
    request_id: str


class RuntimeSettings(ContractModel):
    id: str | None
    revision: int
    base_revision: int | None
    state: RuntimeSettingsState
    values: RuntimeSettingsValues
    created_at: datetime | None
    activated_at: datetime | None


class AISpaceToolResult(ContractModel):
    id: str
    name: str
    role: SpaceRole
    revision: int


class AISpaceListExecution(ContractModel):
    items: list[AISpaceToolResult]
    page: int
    page_size: int
    total_items: int
    total_pages: int


class AISpaceMemberResult(ContractModel):
    member_id: str
    username: str
    display_name: str
    status: MemberStatus
    role: SpaceRole


class AISourceResult(ContractModel):
    id: str
    space_id: str
    display_name: str
    revision: int
    status: DocumentStatus
    original_filename: str | None
    size_bytes: int | None
    updated_at: datetime


class AIIngestionJobResult(ContractModel):
    id: str
    space_id: str
    document_id: str
    state: IngestionJobState
    progress: int
    attempt_count: int
    max_attempts: int
    last_error_code: str | None
    updated_at: datetime


class AIConfirmationRequired(ContractModel):
    status: Literal["confirmation_required"]
    pending_action_id: str
    tool_name: Literal[
        "spaces.archive.v1",
        "spaces.members.set.v1",
        "knowledge.archive.v1",
        "ingestion_jobs.cancel.v1",
        "ingestion_jobs.retry.v1",
        "settings.propose.v1",
    ]
    expires_at: datetime


class AISpaceCreateExecution(ContractModel):
    status: Literal["executed"]
    tool_name: Literal["spaces.create.v1"]
    result: AISpaceToolResult


class AISpaceMembersExecution(ContractModel):
    status: Literal["executed"]
    tool_name: Literal["spaces.members.list.v1"]
    result: Page[AISpaceMemberResult]


class AIRetrievalExecution(ContractModel):
    status: Literal["executed"]
    tool_name: Literal["knowledge.search.v1", "retrieval.explain.v1"]
    result: RetrievalResult


class AIKnowledgeExecution(ContractModel):
    status: Literal["executed"]
    tool_name: Literal["knowledge.read.v1", "knowledge.create.v1", "knowledge.update.v1"]
    result: KnowledgeDetail


class AISourcesExecution(ContractModel):
    status: Literal["executed"]
    tool_name: Literal["sources.list.v1"]
    result: Page[AISourceResult]


class AIIngestionJobsExecution(ContractModel):
    status: Literal["executed"]
    tool_name: Literal["ingestion_jobs.list.v1"]
    result: Page[AIIngestionJobResult]


class AISettingsExecution(ContractModel):
    status: Literal["executed"]
    tool_name: Literal["settings.inspect.v1"]
    result: RuntimeSettings


AIToolExecution = (
    AIConfirmationRequired
    | AISpaceListExecution
    | AISpaceCreateExecution
    | AISpaceMembersExecution
    | AIRetrievalExecution
    | AIKnowledgeExecution
    | AISourcesExecution
    | AIIngestionJobsExecution
    | AISettingsExecution
)
