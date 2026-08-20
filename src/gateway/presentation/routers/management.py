from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.security.tokens import APIKeyCodec, SecretValue
from src.gateway.application.services.api_key_service import APIKeyService
from src.gateway.application.services.ai_management_service import AIManagementService
from src.gateway.application.services.authorized_retrieval_service import AuthorizedRetrievalService
from src.gateway.application.services.document_upload_service import DocumentUploadService
from src.gateway.application.services.idempotency_service import IdempotencyService, ReservationStatus
from src.gateway.application.services.knowledge_management_service import KnowledgeManagementService
from src.gateway.application.services.member_administration_service import MemberAdministrationService
from src.gateway.application.services.runtime_settings_service import RuntimeSettingsService, RuntimeSettingsRevision, RuntimeSettingsValues
from src.gateway.application.services.session_service import SessionService
from src.gateway.application.services.space_service import SpaceService
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import AuthenticationException, AuthorizationException, ResourceConflictException, ValidationException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.domain.entities import KnowledgeItem as DomainKnowledgeItem
from src.gateway.infrastructure.database import get_db_session, get_session_factory
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionModel, IngestionJobModel
from src.gateway.infrastructure.persistence.models import KnowledgeItem as KnowledgeItemModel, KnowledgeRevision as KnowledgeRevisionModel
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from src.gateway.infrastructure.persistence.identity_models import APIKeyScopeModel, AuditEventModel, MemberModel, PendingAIActionModel, PersonalAPIKeyModel, SessionCredentialModel, SessionFamilyModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.infrastructure.persistence.runtime_settings_models import RuntimeSettingRevisionModel
from src.gateway.infrastructure.runtime_settings_provider import load_active_retrieval_settings
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from src.gateway.presentation.authorization import require_scope
from src.gateway.presentation.request_context import get_request_id


router = APIRouter(prefix="/api/v1", tags=["Management"])
KnowledgeTag = Annotated[str, Field(min_length=1, max_length=80)]


class SpaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)


class MembershipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: SpaceRole


class APIKeyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(min_length=1, max_length=16)
    expires_at: datetime | None = None


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


class KnowledgeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=1_000_000)
    tags: list[KnowledgeTag] = Field(default_factory=list, max_length=32)
    change_summary: str | None = Field(default=None, max_length=500)


class RetrievalSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    space_ids: list[str] | None = Field(default=None, max_length=100)
    active_space_id: str | None = Field(default=None, max_length=64)
    semantic_policy: Literal["prefer", "required", "disabled"] = "prefer"
    limit: int = Field(default=20, ge=1, le=50)


class RuntimeSettingsDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(ge=0)
    reason: str = Field(min_length=5, max_length=500)
    values: RuntimeSettingsValues


class RuntimeSettingsActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_active_revision: int = Field(ge=0)
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


class AIToolExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict


def _actor_id(principal: Principal) -> UUID:
    try:
        return UUID(principal.subject_id)
    except ValueError as exc:
        raise AuthorizationException() from exc


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


def _cursor_encode(value: str) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"after": value}, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


def _cursor_decode(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")
        )
        after = payload["after"]
        if not isinstance(after, str) or not after:
            raise ValueError
        return after
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationException("Invalid pagination cursor.") from exc


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


def _key_codec() -> APIKeyCodec:
    gateway = get_settings().gateway
    if not gateway.api_key_peppers:
        raise RuntimeError("API key peppers are unavailable")
    return APIKeyCodec(
        {
            version: SecretValue(value)
            for version, value in gateway.api_key_peppers.items()
        },
        active_pepper_version=gateway.active_api_key_pepper_version,
    )


def _settings_payload(revision: RuntimeSettingsRevision) -> dict:
    return {
        "id": str(revision.id) if revision.id else None,
        "revision": revision.revision,
        "base_revision": revision.base_revision,
        "state": revision.state,
        "values": revision.values.model_dump(mode="json"),
        "created_at": revision.created_at.isoformat() if revision.created_at else None,
        "activated_at": revision.activated_at.isoformat() if revision.activated_at else None,
    }


def _knowledge_payload(item: DomainKnowledgeItem, *, include_content: bool = True) -> dict:
    revision = item.current_revision
    content = revision.content if revision else item.content or ""
    payload = {
        "id": str(item.id),
        "space_id": item.workspace_id,
        "title": item.title,
        "content_excerpt": content[:320],
        "tags": list(revision.tags if revision else item.tags),
        "version": revision.version if revision else item.version,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
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


@router.get("/ai-tools")
async def list_ai_tools(
    principal: Principal = Depends(require_scope("spaces:read")),
) -> dict:
    tools = []
    if "*" in principal.scopes or "spaces:read" in principal.scopes:
        tools.append(
            {
                "name": "spaces.list.v1",
                "description": "List only active spaces accessible to the current member.",
                "confirmation": "none",
                "parameters": AIListSpacesArguments.model_json_schema(),
            }
        )
    if "*" in principal.scopes or "spaces:write" in principal.scopes:
        tools.extend(
            [
                {
                    "name": "spaces.create.v1",
                    "description": "Create a private space owned by the current member.",
                    "confirmation": "none",
                    "parameters": AICreateSpaceArguments.model_json_schema(),
                },
                {
                    "name": "spaces.archive.v1",
                    "description": "Propose archiving an owned non-global space. A website confirmation is required before execution.",
                    "confirmation": "required",
                    "parameters": AIArchiveSpaceArguments.model_json_schema(),
                },
            ]
        )
    return {"items": tools}


@router.post("/ai-tools/{tool_name}", status_code=status.HTTP_202_ACCEPTED)
async def execute_ai_tool(
    tool_name: str,
    payload: AIToolExecutionRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("spaces:write")),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    if tool_name not in {"spaces.list.v1", "spaces.create.v1", "spaces.archive.v1"}:
        raise ValidationException("Unknown or unavailable AI tool.")
    try:
        if tool_name == "spaces.list.v1":
            arguments = AIListSpacesArguments.model_validate(payload.arguments)
        elif tool_name == "spaces.create.v1":
            arguments = AICreateSpaceArguments.model_validate(payload.arguments)
        else:
            arguments = AIArchiveSpaceArguments.model_validate(payload.arguments)
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
    if tool_name == "spaces.list.v1":
        rows = list(
            await session.execute(
                select(Workspace, SpaceMembershipModel.role)
                .join(SpaceMembershipModel, SpaceMembershipModel.space_id == Workspace.id)
                .where(
                    SpaceMembershipModel.member_id == _actor_id(principal),
                    Workspace.archived_at.is_(None),
                )
                .order_by(Workspace.name, Workspace.id)
                .limit(arguments.limit)
            )
        )
        result = [
            {"id": space.id, "name": space.name, "role": role, "revision": space.revision}
            for space, role in rows
        ]
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=[space["id"] for space in result],
        )
        return JSONResponse(content={"items": result, "next_cursor": None})
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
        return JSONResponse(status_code=201, content={"status": "executed", "tool_name": tool_name, "result": result})
    if reservation.status is ReservationStatus.REPLAY:
        action = await session.get(PendingAIActionModel, UUID(reservation.resource_ids[0]))
        if action is None:
            raise ResourceConflictException("The pending AI action is unavailable.")
        pending_id = action.id
        expires_at = action.expires_at
    else:
        pending = await AIManagementService(session).propose_space_archive(
            principal,
            space_id=arguments.space_id,
            expected_revision=arguments.expected_revision,
            request_id=get_request_id(request),
        )
        pending_id = pending.action_id
        expires_at = pending.expires_at
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=202,
            resource_ids=[str(pending_id)],
        )
    return JSONResponse(
        status_code=202,
        content={
            "status": "confirmation_required",
            "pending_action_id": str(pending_id),
            "tool_name": tool_name,
            "expires_at": expires_at.isoformat(),
        },
    )


@router.post("/ai-actions/{action_id}/confirm")
async def confirm_ai_action(
    action_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("spaces:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    if principal.kind is not PrincipalKind.SESSION:
        raise AuthorizationException()
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


@router.get("/ai-actions")
async def list_ai_actions(
    principal: Principal = Depends(require_scope("spaces:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    if principal.kind is not PrincipalKind.SESSION:
        raise AuthorizationException()
    current_time = datetime.now(timezone.utc)
    actions = list(
        await session.scalars(
            select(PendingAIActionModel)
            .where(
                PendingAIActionModel.actor_member_id == _actor_id(principal),
                PendingAIActionModel.state == "pending",
                PendingAIActionModel.expires_at > current_time,
            )
            .order_by(PendingAIActionModel.created_at.desc())
            .limit(100)
        )
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
        "next_cursor": None,
    }


@router.get("/me")
async def me(
    principal: Principal = Depends(require_scope("spaces:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    member = await session.get(MemberModel, _actor_id(principal))
    if member is None or member.status == MemberStatus.DISABLED.value:
        raise AuthorizationException()
    return {
        "id": str(member.id),
        "username": member.username,
        "display_name": member.display_name,
        "status": member.status,
        "system_role": member.system_role,
        "requires_password_change": member.force_password_change,
    }


@router.get("/spaces")
async def list_spaces(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(require_scope("spaces:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    member_id = _actor_id(principal)
    after = _cursor_decode(cursor)
    query = (
        select(Workspace, SpaceMembershipModel.role)
        .join(SpaceMembershipModel, SpaceMembershipModel.space_id == Workspace.id)
        .where(
            SpaceMembershipModel.member_id == member_id,
            Workspace.archived_at.is_(None),
        )
        .order_by(Workspace.id)
        .limit(limit + 1)
    )
    if after is not None:
        query = query.where(Workspace.id > after)
    rows = (await session.execute(query)).all()
    page = rows[:limit]
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
            for space, role in page
        ],
        "next_cursor": _cursor_encode(page[-1][0].id) if len(rows) > limit else None,
    }


@router.post("/spaces", status_code=status.HTTP_201_CREATED)
async def create_space(
    payload: SpaceCreateRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("spaces:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
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


@router.get("/spaces/{space_id}/members")
async def list_space_members(
    space_id: str,
    principal: Principal = Depends(require_scope("spaces:members")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    actor_membership = await session.scalar(
        select(SpaceMembershipModel).where(
            SpaceMembershipModel.space_id == space_id,
            SpaceMembershipModel.member_id == _actor_id(principal),
        )
    )
    if actor_membership is None or actor_membership.role != SpaceRole.OWNER.value:
        raise AuthorizationException()
    rows = (
        await session.execute(
            select(SpaceMembershipModel, MemberModel)
            .join(MemberModel, MemberModel.id == SpaceMembershipModel.member_id)
            .where(SpaceMembershipModel.space_id == space_id)
            .order_by(MemberModel.username_normalized)
        )
    ).all()
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
        "next_cursor": None,
    }


@router.get("/spaces/{space_id}/member-candidates")
async def list_space_member_candidates(
    space_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(require_scope("spaces:members")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    actor_membership = await session.scalar(
        select(SpaceMembershipModel).where(
            SpaceMembershipModel.space_id == space_id,
            SpaceMembershipModel.member_id == _actor_id(principal),
        )
    )
    if actor_membership is None or actor_membership.role != SpaceRole.OWNER.value:
        raise AuthorizationException()
    after = _cursor_decode(cursor)
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
        .limit(limit + 1)
    )
    if after is not None:
        query = query.where(MemberModel.username_normalized > after)
    rows = list(await session.scalars(query))
    page = rows[:limit]
    return {
        "items": [
            {
                "member_id": str(member.id),
                "username": member.username,
                "display_name": member.display_name,
            }
            for member in page
        ],
        "next_cursor": _cursor_encode(page[-1].username_normalized) if len(rows) > limit else None,
    }


@router.put("/spaces/{space_id}/members/{member_id}")
async def set_space_membership(
    space_id: str,
    member_id: UUID,
    payload: MembershipRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("spaces:members")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    reservation = await _reserve(
        session,
        principal,
        "space.membership.set",
        idempotency_key,
        {"space_id": space_id, "member_id": str(member_id), "role": payload.role.value},
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await SpaceService(session).set_membership(
            principal,
            space_id,
            member_id,
            role=payload.role,
            request_id=get_request_id(request),
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=200,
            resource_ids=[space_id, str(member_id)],
        )
    return {"space_id": space_id, "member_id": str(member_id), "role": payload.role.value}


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


@router.get("/knowledge")
async def list_knowledge(
    space_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    after = _cursor_decode(cursor)
    query = (
        select(KnowledgeItemModel, KnowledgeRevisionModel)
        .outerjoin(KnowledgeRevisionModel, KnowledgeRevisionModel.id == KnowledgeItemModel.current_revision_id)
        .where(KnowledgeItemModel.is_deleted.is_(False))
        .order_by(KnowledgeItemModel.id)
        .limit(limit + 1)
    )
    if space_id is not None:
        query = query.where(KnowledgeItemModel.workspace_id == space_id)
    if after is not None:
        try:
            query = query.where(KnowledgeItemModel.id > UUID(after))
        except ValueError as exc:
            raise ValidationException("Invalid pagination cursor.") from exc
    rows = (await session.execute(query)).all()
    page = rows[:limit]
    return {
        "items": [_orm_knowledge_payload(item, revision, include_content=False) for item, revision in page],
        "next_cursor": _cursor_encode(str(page[-1][0].id)) if len(rows) > limit else None,
    }


@router.post("/knowledge", status_code=status.HTTP_201_CREATED)
async def create_knowledge(
    payload: KnowledgeCreateRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    reservation = await _reserve(
        session,
        principal,
        "knowledge.create",
        idempotency_key,
        payload.model_dump(mode="json"),
    )
    knowledge = KnowledgeManagementService(session)
    if reservation.status is ReservationStatus.REPLAY:
        if not reservation.resource_ids:
            raise ResourceConflictException()
        existing = await knowledge.get(UUID(reservation.resource_ids[0]))
        if existing is None:
            raise ResourceConflictException()
        return _knowledge_payload(existing)
    created = await knowledge.create(
        principal,
        space_id=payload.space_id,
        title=payload.title.strip(),
        content=payload.content,
        tags=[tag.strip() for tag in payload.tags if tag.strip()],
        request_id=get_request_id(request),
    )
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=201,
        resource_ids=[str(created.id)],
    )
    return _knowledge_payload(created)


@router.get("/knowledge/{item_id}")
async def get_knowledge(
    item_id: UUID,
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    item = await KnowledgeManagementService(session).get(item_id)
    if item is None:
        raise AuthorizationException()
    return _knowledge_payload(item)


@router.put("/knowledge/{item_id}")
async def update_knowledge(
    item_id: UUID,
    payload: KnowledgeUpdateRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    reservation = await _reserve(
        session,
        principal,
        "knowledge.update",
        idempotency_key,
        {"item_id": str(item_id), **payload.model_dump(mode="json")},
    )
    knowledge = KnowledgeManagementService(session)
    if reservation.status is ReservationStatus.REPLAY:
        current = await knowledge.get(item_id)
        if current is None:
            raise ResourceConflictException()
        return _knowledge_payload(current)
    updated = await knowledge.update(
        principal,
        item_id,
        expected_version=payload.expected_version,
        title=payload.title.strip(),
        content=payload.content,
        tags=[tag.strip() for tag in payload.tags if tag.strip()],
        change_summary=payload.change_summary,
        request_id=get_request_id(request),
    )
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=200,
        resource_ids=[str(item_id)],
    )
    return _knowledge_payload(updated)


@router.delete("/knowledge/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge(
    item_id: UUID,
    request: Request,
    expected_version: int = Query(ge=1),
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    reservation = await _reserve(
        session,
        principal,
        "knowledge.delete",
        idempotency_key,
        {"item_id": str(item_id), "expected_version": expected_version},
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await KnowledgeManagementService(session).delete(
            principal,
            item_id,
            expected_version=expected_version,
            request_id=get_request_id(request),
        )
        await IdempotencyService(session).complete(
            reservation.record_id,
            response_status=204,
            resource_ids=[str(item_id)],
        )
    return Response(status_code=204)


@router.post("/retrieval/search")
async def search_retrieval(
    payload: RetrievalSearchRequest,
    principal: Principal = Depends(require_scope("knowledge:read")),
) -> dict:
    result = await AuthorizedRetrievalService(
        PostgresRetrievalUnitRepository(get_session_factory()),
        None,
        runtime_settings_provider=load_active_retrieval_settings,
    ).search(
        principal,
        payload.query,
        requested_space_ids=set(payload.space_ids) if payload.space_ids is not None else None,
        active_space_id=payload.active_space_id,
        semantic_policy=payload.semantic_policy,
        limit=payload.limit,
    )
    return {
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


@router.post("/sources/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_source(
    file: UploadFile = File(...),
    space_id: str = Form(min_length=1, max_length=64),
    display_name: str | None = Form(default=None, max_length=500),
    idempotency_key: str = Header(min_length=1, max_length=255, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("knowledge:write")),
) -> dict:
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
    return {
        "document_id": str(receipt.document_id),
        "revision_id": str(receipt.revision_id),
        "job_id": str(receipt.job_id),
        "job_state": receipt.job_state,
        "duplicate_candidate_revision_id": str(receipt.duplicate_candidate_revision_id) if receipt.duplicate_candidate_revision_id else None,
    }


@router.get("/sources")
async def list_sources(
    space_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    query = select(DocumentModel).where(DocumentModel.archived_at.is_(None)).order_by(DocumentModel.created_at.desc()).limit(limit)
    if space_id is not None:
        query = query.where(DocumentModel.space_id == space_id)
    documents = list(await session.scalars(query))
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
        "next_cursor": None,
    }


@router.get("/ingestion-jobs")
async def list_ingestion_jobs(
    space_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_scope("knowledge:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    query = select(IngestionJobModel).order_by(IngestionJobModel.created_at.desc()).limit(limit)
    if space_id is not None:
        query = query.where(IngestionJobModel.space_id == space_id)
    jobs = list(await session.scalars(query))
    return {
        "items": [
            {
                "id": str(job.id),
                "space_id": job.space_id,
                "document_id": str(job.document_id),
                "document_revision_id": str(job.document_revision_id),
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
        "next_cursor": None,
    }


@router.get("/api-keys")
async def list_api_keys(
    principal: Principal = Depends(require_scope("api_keys:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    member_id = _actor_id(principal)
    keys = list(
        await session.scalars(
            select(PersonalAPIKeyModel)
            .where(PersonalAPIKeyModel.member_id == member_id)
            .order_by(PersonalAPIKeyModel.created_at.desc(), PersonalAPIKeyModel.id.desc())
            .limit(100)
        )
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
    scopes: dict[UUID, list[str]] = {key_id: [] for key_id in key_ids}
    for key_id, scope in scope_rows:
        scopes[key_id].append(scope)
    return {
        "items": [
            {
                "id": str(key.id),
                "public_id": key.public_id,
                "name": key.name,
                "status": key.status,
                "scopes": sorted(scopes[key.id]),
                "created_at": key.created_at.isoformat(),
                "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
                "expires_at": key.expires_at.isoformat() if key.expires_at else None,
                "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
            }
            for key in keys
        ],
        "next_cursor": None,
    }


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    payload: APIKeyCreateRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("api_keys:write")),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
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
        created = await APIKeyService(session, _key_codec()).create(
            _actor_id(principal),
            family_id=await _family_id(session, principal),
            name=payload.name,
            scopes=set(payload.scopes),
            expires_at=payload.expires_at,
            request_id=get_request_id(request),
        )
    except ValueError as exc:
        raise ValidationException(str(exc)) from exc
    await IdempotencyService(session).complete(
        reservation.record_id,
        response_status=201,
        resource_ids=[str(created.key_id)],
    )
    response = JSONResponse(
        status_code=201,
        content={
            "id": str(created.key_id),
            "public_id": created.public_id,
            "secret": created.secret.reveal(),
            "scopes": sorted(created.scopes),
            "expires_at": created.expires_at.isoformat() if created.expires_at else None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("api_keys:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    reservation = await _reserve(
        session,
        principal,
        "api_key.revoke",
        idempotency_key,
        {"key_id": str(key_id)},
    )
    if reservation.status is not ReservationStatus.REPLAY:
        await APIKeyService(session, _key_codec()).revoke(
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


@router.get("/members")
async def list_members(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = Query(default=None),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    if principal.system_role is not SystemRole.SUPER_ADMIN:
        raise AuthorizationException()
    after = _cursor_decode(cursor)
    query = select(MemberModel).order_by(MemberModel.username_normalized).limit(limit + 1)
    if after is not None:
        query = query.where(MemberModel.username_normalized > after)
    rows = list(await session.scalars(query))
    page = rows[:limit]
    return {
        "items": [
            {
                "id": str(member.id),
                "username": member.username,
                "display_name": member.display_name,
                "status": member.status,
                "system_role": member.system_role,
                "requires_password_change": member.force_password_change,
                "created_at": member.created_at.isoformat(),
                "updated_at": member.updated_at.isoformat(),
            }
            for member in page
        ],
        "next_cursor": _cursor_encode(page[-1].username_normalized) if len(rows) > limit else None,
    }


@router.post("/members", status_code=status.HTTP_201_CREATED)
async def create_member(
    payload: MemberCreateRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
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
    response = JSONResponse(
        status_code=201,
        content={
            "id": str(created.member_id),
            "username": created.username,
            "display_name": created.display_name,
            "temporary_password": created.temporary_password.reveal(),
            "temporary_password_expires_at": created.expires_at.isoformat(),
            "requires_password_change": True,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.patch("/members/{member_id}")
async def update_member(
    member_id: UUID,
    payload: MemberUpdateRequest,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
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
    return {
        "id": str(member.id),
        "username": member.username,
        "display_name": member.display_name,
        "status": member.status,
        "system_role": member.system_role,
        "requires_password_change": member.force_password_change,
    }


@router.post("/members/{member_id}/password-reset")
async def reset_member_password(
    member_id: UUID,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128, alias="Idempotency-Key"),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
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
    response = JSONResponse(
        content={
            "id": str(reset.member_id),
            "temporary_password": reset.temporary_password.reveal(),
            "temporary_password_expires_at": reset.expires_at.isoformat(),
            "requires_password_change": True,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/sessions")
async def list_sessions(
    principal: Principal = Depends(require_scope("sessions:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    current_family = await _family_id(session, principal)
    rows = list(
        await session.scalars(
            select(SessionFamilyModel)
            .where(SessionFamilyModel.member_id == _actor_id(principal))
            .order_by(SessionFamilyModel.created_at.desc())
            .limit(100)
        )
    )
    current_time = datetime.now(timezone.utc)
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
        "next_cursor": None,
    }


@router.delete("/sessions/{family_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    family_id: UUID,
    principal: Principal = Depends(require_scope("sessions:write")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    family = await session.get(SessionFamilyModel, family_id)
    if family is None or family.member_id != _actor_id(principal):
        raise AuthorizationException()
    await SessionService(session).revoke_family(family_id, reason="member_revoked")
    return Response(status_code=204)


@router.get("/audit-events")
async def list_audit_events(
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(require_scope("members:write")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    if principal.system_role is not SystemRole.SUPER_ADMIN:
        raise AuthorizationException()
    rows = list(
        await session.scalars(
            select(AuditEventModel)
            .order_by(AuditEventModel.occurred_at.desc(), AuditEventModel.id.desc())
            .limit(limit)
        )
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
        "next_cursor": None,
    }


@router.get("/settings")
async def active_runtime_settings(
    principal: Principal = Depends(require_scope("settings:read")),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    return _settings_payload(await RuntimeSettingsService(session).active())


@router.post("/settings/drafts", status_code=status.HTTP_201_CREATED)
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


@router.post("/settings/drafts/{draft_id}/activate")
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
        activated = await RuntimeSettingsService(session).activate(
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
