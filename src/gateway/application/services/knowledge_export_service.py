from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.use_cases.context import UseCaseContext, actor_id
from src.gateway.domain.authorization import Action
from src.gateway.domain.entities import KnowledgeRevision as DomainKnowledgeRevision
from src.gateway.domain.exceptions import AuthorizationException, ValidationException
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import KnowledgeItem as KnowledgeItemModel
from src.gateway.infrastructure.persistence.models import KnowledgeRevision as KnowledgeRevisionModel


EXPORT_FORMAT = "openknowledge-knowledge-export"
EXPORT_VERSION = 1

EXPORT_REQUIRED_TOP_KEYS = frozenset({"format", "version", "space_id", "items"})
EXPORT_REQUIRED_ITEM_KEYS = frozenset({"id", "title", "tags", "revisions"})
EXPORT_REQUIRED_REVISION_KEYS = frozenset({"id", "version", "title", "content", "tags"})

EXPORT_LIFECYCLE_STATUSES = frozenset({"observation", "candidate", "accepted", "superseded"})


def knowledge_export_payload(
    *,
    space_id: str,
    items: list[dict[str, Any]],
    exported_by: str,
) -> dict[str, Any]:
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "space_id": space_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by": exported_by,
        "items": items,
    }


def item_export_payload(
    item: KnowledgeItemModel,
    revisions: list[KnowledgeRevisionModel],
) -> dict[str, Any]:
    ordered = sorted(revisions, key=lambda revision: revision.version)
    return {
        "id": str(item.id),
        "title": item.title,
        "tags": list(item.tags or []),
        "lifecycle_status": item.lifecycle_status,
        "origin": item.origin,
        "source_detail": item.source_detail,
        "review_note": item.review_note,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "revisions": [
            {
                "id": str(revision.id),
                "version": revision.version,
                "title": revision.title,
                "content": revision.content,
                "tags": list(revision.tags or []),
                "change_summary": revision.change_summary,
                "created_at": revision.created_at.isoformat() if revision.created_at else None,
            }
            for revision in ordered
        ],
    }


def validate_export_document(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValidationException("Export document must be an object.")
    missing = EXPORT_REQUIRED_TOP_KEYS - set(document.keys())
    if missing:
        raise ValidationException("Export document is missing required keys.")
    if document.get("format") != EXPORT_FORMAT:
        raise ValidationException("Unsupported export format.")
    if document.get("version") != EXPORT_VERSION:
        raise ValidationException("Unsupported export version.")
    space_id = document.get("space_id")
    if not isinstance(space_id, str) or not space_id.strip():
        raise ValidationException("Export document has an invalid space.")
    items = document.get("items")
    if not isinstance(items, list):
        raise ValidationException("Export document items must be a list.")
    for item in items:
        _validate_export_item(item)
    return document


def _validate_export_item(item: Any) -> None:
    if not isinstance(item, dict):
        raise ValidationException("Export item must be an object.")
    if EXPORT_REQUIRED_ITEM_KEYS - set(item.keys()):
        raise ValidationException("Export item is missing required keys.")
    _parse_uuid(item.get("id"), field_name="item id")
    title = item.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise ValidationException("Export item has an invalid title.")
    tags = item.get("tags")
    if (
        not isinstance(tags, list)
        or len(tags) > 32
        or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 80 for tag in tags)
    ):
        raise ValidationException("Export item has invalid tags.")
    status = item.get("lifecycle_status", "accepted")
    if status not in EXPORT_LIFECYCLE_STATUSES:
        raise ValidationException("Export item has an invalid lifecycle status.")
    for key, limit in (("origin", 200), ("source_detail", 2000), ("review_note", 2000)):
        value = item.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise ValidationException("Export item has invalid lifecycle metadata.")
    expires_at = item.get("expires_at")
    if expires_at is not None:
        _parse_datetime(expires_at, field_name="expiry")
    revisions = item.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        raise ValidationException("Export item must include at least one revision.")
    seen_versions: set[int] = set()
    for revision in revisions:
        _validate_export_revision(revision, seen_versions)


def _validate_export_revision(revision: Any, seen_versions: set[int]) -> None:
    if not isinstance(revision, dict):
        raise ValidationException("Export revision must be an object.")
    if EXPORT_REQUIRED_REVISION_KEYS - set(revision.keys()):
        raise ValidationException("Export revision is missing required keys.")
    _parse_uuid(revision.get("id"), field_name="revision id")
    version = revision.get("version")
    if not isinstance(version, int) or version < 1:
        raise ValidationException("Export revision has an invalid version.")
    if version in seen_versions:
        raise ValidationException("Export revision versions must be unique.")
    seen_versions.add(version)
    content = revision.get("content")
    if not isinstance(content, str) or not content or len(content) > 1_000_000:
        raise ValidationException("Export revision has invalid content.")
    title = revision.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > 500):
        raise ValidationException("Export revision has an invalid title.")
    tags = revision.get("tags")
    if (
        not isinstance(tags, list)
        or len(tags) > 32
        or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 80 for tag in tags)
    ):
        raise ValidationException("Export revision has invalid tags.")


def _parse_uuid(value: Any, *, field_name: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError as exc:
        raise ValidationException(f"Export document has an invalid {field_name}.") from exc


def _parse_datetime(value: Any, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationException(f"Export document has an invalid {field_name}.") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


class KnowledgeExportService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def export_space(self, ctx: UseCaseContext, space_id: str) -> dict[str, Any]:
        await AuthorizationService(self._session).authorize_space(ctx.principal, space_id, Action.CONTENT_READ)
        items = list(
            await self._session.scalars(
                select(KnowledgeItemModel)
                .where(
                    KnowledgeItemModel.workspace_id == space_id,
                    KnowledgeItemModel.is_deleted.is_(False),
                )
                .order_by(KnowledgeItemModel.created_at.asc(), KnowledgeItemModel.id.asc())
            )
        )
        payload_items = []
        for item in items:
            revisions = list(
                await self._session.scalars(
                    select(KnowledgeRevisionModel)
                    .where(KnowledgeRevisionModel.item_id == item.id)
                    .order_by(KnowledgeRevisionModel.version.asc())
                )
            )
            payload_items.append(item_export_payload(item, revisions))
        return knowledge_export_payload(
            space_id=space_id,
            items=payload_items,
            exported_by=ctx.principal.subject_id,
        )

    async def import_space(
        self,
        ctx: UseCaseContext,
        space_id: str,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        validated = validate_export_document(document)
        if validated.get("space_id") != space_id:
            raise ValidationException("Export space does not match the target space.")
        await AuthorizationService(self._session).authorize_space(ctx.principal, space_id, Action.CONTENT_WRITE)
        now = datetime.now(timezone.utc)
        created = 0
        skipped = 0
        for item in validated["items"]:
            item_id = UUID(str(item["id"]))
            existing = await self._session.get(KnowledgeItemModel, item_id)
            if existing is not None:
                skipped += 1
                continue
            ordered = sorted(item["revisions"], key=lambda revision: revision["version"])
            revision_ids = [UUID(str(revision["id"])) for revision in ordered]
            clashing = await self._session.scalars(
                select(KnowledgeRevisionModel.id).where(KnowledgeRevisionModel.id.in_(revision_ids))
            )
            if list(clashing):
                raise ValidationException("Export revision collides with an existing revision.")
            first = ordered[0]
            expires_value = item.get("expires_at")
            expires_at = _parse_datetime(expires_value, field_name="expiry") if expires_value is not None else None
            record = KnowledgeItemModel(
                id=item_id,
                workspace_id=space_id,
                title=item.get("title") or first.get("title") or "",
                content=first["content"],
                tags=list(item.get("tags") or []),
                current_revision_id=None,
                lifecycle_status=item.get("lifecycle_status", "accepted"),
                origin=item.get("origin"),
                source_detail=item.get("source_detail"),
                review_note=item.get("review_note"),
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
            self._session.add(record)
            await self._session.flush()
            for revision in ordered:
                revision_id = UUID(str(revision["id"]))
                self._session.add(
                    KnowledgeRevisionModel(
                        id=revision_id,
                        item_id=item_id,
                        space_id=space_id,
                        version=revision["version"],
                        title=revision.get("title"),
                        content_hash=DomainKnowledgeRevision.compute_hash(revision["content"]),
                        content=revision["content"],
                        tags=list(revision.get("tags") or []),
                        change_summary=revision.get("change_summary"),
                        author=ctx.principal.subject_id,
                        author_member_id=actor_id(ctx.principal),
                        created_at=now,
                    )
                )
            await self._session.flush()
            current = max(ordered, key=lambda revision: revision["version"])
            record.current_revision_id = UUID(str(current["id"]))
            record.revision = current["version"]
            await self._session.flush()
            generation = await self._session.scalar(
                select(EmbeddingGenerationModel).where(
                    EmbeddingGenerationModel.purpose == "retrieval",
                    EmbeddingGenerationModel.status == "active",
                )
            )
            if generation is not None:
                self._session.add(
                    RetrievalUnitModel(
                        space_id=space_id,
                        source_type="knowledge_revision",
                        knowledge_revision_id=UUID(str(current["id"])),
                        embedding_generation_id=generation.id,
                        title=current.get("title") or record.title,
                        content=current["content"],
                        language=None,
                        source_metadata={
                            "knowledge_item_id": str(item_id),
                            "knowledge_revision_id": str(current["id"]),
                            "version": current["version"],
                            "tags": list(current.get("tags") or []),
                        },
                        embedding=None,
                        active=True,
                    )
                )
                await self._session.flush()
            created += 1
        return {"space_id": space_id, "created": created, "skipped": skipped}


def import_summary_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "space_id": result["space_id"],
        "created": result["created"],
        "skipped": result["skipped"],
    }
