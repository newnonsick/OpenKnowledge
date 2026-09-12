from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.services.runtime_settings_service import RuntimeSettingsService
from src.gateway.domain.authorization import Action, AuthorizationContext, is_allowed
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.persistence.identity_models import MemberModel, PendingAIActionModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import IngestionJobModel
from src.gateway.infrastructure.persistence.models import KnowledgeItem, KnowledgeRevision, Workspace


REDACTED = "[redacted]"


def _role_label(role: str | None) -> str:
    if role is None:
        return "removed"
    return str(role)


def _summarize_settings_change(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for group in ("retrieval", "chunking", "tools", "features"):
        if before.get(group) != after.get(group):
            changed.append(group)
    return changed


def build_action_review(
    tool_name: str,
    command: dict[str, Any],
    live: dict[str, Any],
    *,
    include_sensitive: bool,
) -> dict[str, Any]:
    if tool_name == "spaces.archive.v1":
        space_name = live.get("space_name") or command.get("space_id")
        return {
            "summary": f"Archive space '{space_name}'",
            "change": {
                "kind": "space_archive",
                "before": {"space_id": command.get("space_id"), "archived": False},
                "after": {"space_id": command.get("space_id"), "archived": True},
            },
            "impact": "The space leaves unified search immediately while its history remains preserved.",
            "redacted": [],
        }
    if tool_name == "spaces.members.set.v1":
        before_role = live.get("current_role")
        after_role = command.get("role")
        target_name = live.get("target_name") or command.get("member_id")
        space_name = live.get("space_name") or command.get("space_id")
        return {
            "summary": (
                f"Change {target_name} in '{space_name}' "
                f"from {_role_label(before_role)} to {_role_label(after_role)}"
            ),
            "change": {
                "kind": "membership",
                "before": {"member_id": command.get("member_id"), "role": before_role},
                "after": {"member_id": command.get("member_id"), "role": after_role},
            },
            "impact": (
                "This changes who can access this space and which actions that member can perform."
                if after_role is not None
                else "This immediately revokes the member's access to this space and its search results."
            ),
            "redacted": [],
        }
    if tool_name == "knowledge.archive.v1":
        title = live.get("title") if include_sensitive else REDACTED
        return {
            "summary": f"Archive knowledge '{title}'",
            "change": {
                "kind": "knowledge_archive",
                "before": {
                    "item_id": command.get("item_id"),
                    "title": title,
                    "version": live.get("version"),
                },
                "after": {
                    "item_id": command.get("item_id"),
                    "title": title,
                    "version": live.get("version"),
                    "archived": True,
                },
            },
            "impact": "It will leave default retrieval but its revision history remains preserved.",
            "redacted": [] if include_sensitive else ["title"],
        }
    if tool_name in {"ingestion_jobs.cancel.v1", "ingestion_jobs.retry.v1"}:
        operation = "cancel" if tool_name.endswith("cancel.v1") else "retry"
        return {
            "summary": f"{operation.capitalize()} ingestion job {command.get('job_id')}",
            "change": {
                "kind": f"ingestion_job_{operation}",
                "before": {"job_id": command.get("job_id"), "state": live.get("state")},
                "after": {
                    "job_id": command.get("job_id"),
                    "state": live.get("state"),
                    "requested": operation,
                },
            },
            "impact": (
                "The worker will stop at a safe boundary; completed durable stages and audit history remain preserved."
                if operation == "cancel"
                else "This starts a new durable attempt from the preserved original source."
            ),
            "redacted": [],
        }
    if tool_name == "settings.propose.v1":
        proposed = command.get("values") if include_sensitive else REDACTED
        changed = (
            _summarize_settings_change(live.get("active_values") or {}, command.get("values") or {})
            if include_sensitive
            else []
        )
        return {
            "summary": f"Propose runtime settings draft from revision {command.get('base_revision')}",
            "change": {
                "kind": "settings_proposal",
                "before": {"base_revision": command.get("base_revision")},
                "after": {
                    "base_revision": command.get("base_revision"),
                    "values": proposed,
                    "changed_groups": changed,
                    "reason": command.get("reason"),
                },
            },
            "impact": "This creates a validated settings draft only. Activation remains a separate recent-step-up action.",
            "redacted": [] if include_sensitive else ["values"],
        }
    return {
        "summary": f"Execute {tool_name}",
        "change": {"kind": "unknown", "before": {}, "after": {}},
        "impact": "Approval is bound to this exact command, target, member, current state, and expiration.",
        "redacted": [],
    }


async def review_pending_action(
    session: AsyncSession,
    viewer: Principal,
    action_id: UUID,
) -> dict[str, Any]:
    try:
        viewer_id = UUID(viewer.subject_id)
    except ValueError as exc:
        raise AuthorizationException() from exc
    action = await session.get(PendingAIActionModel, action_id)
    if action is None or action.actor_member_id != viewer_id:
        raise AuthorizationException()
    command = dict(action.normalized_command or {})
    tool_name = action.tool_name
    live = await _live_state(session, viewer, tool_name, command)
    include_sensitive = _may_see_sensitive(viewer, tool_name, live)
    review = build_action_review(tool_name, command, live, include_sensitive=include_sensitive)
    return {
        "id": str(action.id),
        "tool_name": tool_name,
        "target_ids": list(action.target_ids or []),
        "expected_revision": action.expected_revision,
        "command_hash": action.command_hash,
        "arguments": command,
        "review": review,
        "created_at": action.created_at.isoformat() if action.created_at else None,
        "expires_at": action.expires_at.isoformat() if action.expires_at else None,
        "status": action.state,
    }


def _may_see_sensitive(viewer: Principal, tool_name: str, live: dict[str, Any]) -> bool:
    if tool_name == "settings.propose.v1":
        return is_allowed(AuthorizationContext(viewer), Action.MEMBER_ADMIN)
    return bool(live.get("authorized", True))


async def _live_state(
    session: AsyncSession,
    viewer: Principal,
    tool_name: str,
    command: dict[str, Any],
) -> dict[str, Any]:
    if tool_name == "spaces.archive.v1":
        space = await session.get(Workspace, str(command.get("space_id")))
        if space is None:
            return {"authorized": False}
        authorized = await _can_read_space(session, viewer, space.id)
        return {
            "authorized": authorized,
            "space_name": space.name if authorized else None,
            "revision": space.revision,
            "archived": space.archived_at is not None,
        }
    if tool_name == "spaces.members.set.v1":
        space = await session.get(Workspace, str(command.get("space_id")))
        target = await _member(session, command.get("member_id"))
        membership = await session.scalar(
            select(SpaceMembershipModel).where(
                SpaceMembershipModel.space_id == str(command.get("space_id")),
                SpaceMembershipModel.member_id == target,
            )
        ) if target is not None else None
        authorized = space is not None and await _can_manage_space(session, viewer, space.id)
        members_visible = authorized and target is not None
        try:
            target_member = await session.get(MemberModel, target) if target is not None else None
        except Exception:
            target_member = None
        return {
            "authorized": authorized,
            "space_name": space.name if authorized and space is not None else None,
            "target_name": target_member.display_name if members_visible and target_member else None,
            "current_role": membership.role if members_visible and membership else None,
            "space_revision": space.revision if space is not None else None,
        }
    if tool_name == "knowledge.archive.v1":
        item_id = _as_uuid(command.get("item_id"))
        row = (
            await session.execute(
                select(KnowledgeItem, KnowledgeRevision)
                .join(KnowledgeRevision, KnowledgeRevision.id == KnowledgeItem.current_revision_id)
                .where(KnowledgeItem.id == item_id, KnowledgeItem.is_deleted.is_(False))
            )
        ).one_or_none() if item_id is not None else None
        if row is None:
            return {"authorized": False}
        item, revision = row
        authorized = await _can_read_space(session, viewer, item.workspace_id)
        return {
            "authorized": authorized,
            "title": revision.title or item.title if authorized else None,
            "version": revision.version,
            "space_id": item.workspace_id,
        }
    if tool_name in {"ingestion_jobs.cancel.v1", "ingestion_jobs.retry.v1"}:
        job_id = _as_uuid(command.get("job_id"))
        job = await session.get(IngestionJobModel, job_id) if job_id is not None else None
        if job is None:
            return {"authorized": False}
        authorized = await _can_read_space(session, viewer, job.space_id)
        return {"authorized": authorized, "state": job.state, "space_id": job.space_id}
    if tool_name == "settings.propose.v1":
        active = await RuntimeSettingsService(session).active()
        values = active.values.model_dump(mode="json")
        return {"authorized": True, "active_values": values, "active_revision": active.revision}
    return {"authorized": True}


async def _can_read_space(session: AsyncSession, viewer: Principal, space_id: str) -> bool:
    try:
        await AuthorizationService(session).authorize_space(viewer, space_id, Action.CONTENT_READ)
        return True
    except AuthorizationException:
        return False


async def _can_manage_space(session: AsyncSession, viewer: Principal, space_id: str) -> bool:
    try:
        await AuthorizationService(session).authorize_space(viewer, space_id, Action.MEMBERSHIP_MANAGE)
        return True
    except AuthorizationException:
        return False


async def _member(session: AsyncSession, raw: Any) -> UUID | None:
    candidate = _as_uuid(raw)
    if candidate is None:
        return None
    model = await session.get(MemberModel, candidate)
    return model.id if model is not None else None


def _as_uuid(raw: Any) -> UUID | None:
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None
