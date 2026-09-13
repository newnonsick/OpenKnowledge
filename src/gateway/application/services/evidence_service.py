from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.use_cases.context import UseCaseContext
from src.gateway.domain.authorization import Action
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException, ValidationException
from src.gateway.infrastructure.persistence.ingestion_models import (
    DocumentModel,
    DocumentRevisionChunkModel,
    DocumentRevisionModel,
)
from src.gateway.infrastructure.persistence.models import KnowledgeItem as KnowledgeItemModel
from src.gateway.infrastructure.persistence.models import KnowledgeRevision as KnowledgeRevisionModel


EvidenceKind = Literal["knowledge_revision", "document_chunk"]


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    kind: EvidenceKind
    space_id: str
    canonical_id: UUID
    revision_id: UUID
    chunk_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class EvidencePayload:
    kind: EvidenceKind
    space_id: str
    canonical_id: str
    revision_id: str
    chunk_id: str | None
    title: str
    content: str
    version: int | None
    superseded: bool
    citation_uri: str


def parse_citation_uri(value: str) -> EvidenceReference:
    prefix = "openknowledge://spaces/"
    if not value.startswith(prefix):
        raise ValidationException("Unknown citation URI.")
    parts = [unquote(part) for part in value[len(prefix):].split("/")]
    try:
        if len(parts) == 5 and parts[1] == "knowledge" and parts[3] == "revisions":
            return EvidenceReference("knowledge_revision", parts[0], UUID(parts[2]), UUID(parts[4]))
        if len(parts) in (5, 7) and parts[1] == "documents" and parts[3] == "revisions":
            chunk = UUID(parts[6]) if len(parts) == 7 and parts[5] == "chunks" else None
            if len(parts) == 7 and chunk is None:
                raise ValidationException("Unknown citation URI.")
            return EvidenceReference("document_chunk", parts[0], UUID(parts[2]), UUID(parts[4]), chunk)
    except (ValueError, IndexError) as exc:
        raise ValidationException("Unknown citation URI.") from exc
    raise ValidationException("Unknown citation URI.")


class EvidenceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def fetch(self, ctx: UseCaseContext, reference: EvidenceReference) -> EvidencePayload:
        if reference.kind == "knowledge_revision":
            return await self._fetch_knowledge(ctx, reference)
        return await self._fetch_document(ctx, reference)

    async def resolve(self, ctx: UseCaseContext, citation_uri: str) -> EvidencePayload:
        return await self.fetch(ctx, parse_citation_uri(citation_uri))

    async def _fetch_knowledge(self, ctx: UseCaseContext, reference: EvidenceReference) -> EvidencePayload:
        revision = await self._session.get(KnowledgeRevisionModel, reference.revision_id)
        if revision is None or revision.item_id != reference.canonical_id or revision.space_id != reference.space_id:
            raise AuthorizationException()
        await AuthorizationService(self._session).authorize_space(ctx.principal, revision.space_id, Action.CONTENT_READ)
        item = await self._session.get(KnowledgeItemModel, reference.canonical_id)
        if item is None or item.is_deleted:
            raise ResourceConflictException("The cited knowledge is no longer available.")
        return EvidencePayload(
            kind="knowledge_revision",
            space_id=revision.space_id,
            canonical_id=str(revision.item_id),
            revision_id=str(revision.id),
            chunk_id=None,
            title=revision.title or item.title,
            content=revision.content,
            version=revision.version,
            superseded=item.current_revision_id != revision.id or item.lifecycle_status == "superseded",
            citation_uri=(
                f"openknowledge://spaces/{revision.space_id}/knowledge/{revision.item_id}"
                f"/revisions/{revision.id}"
            ),
        )

    async def _fetch_document(self, ctx: UseCaseContext, reference: EvidenceReference) -> EvidencePayload:
        revision = await self._session.get(DocumentRevisionModel, reference.revision_id)
        if (
            revision is None
            or revision.document_id != reference.canonical_id
            or revision.space_id != reference.space_id
        ):
            raise AuthorizationException()
        await AuthorizationService(self._session).authorize_space(ctx.principal, revision.space_id, Action.CONTENT_READ)
        document = await self._session.get(DocumentModel, reference.canonical_id)
        if document is None or document.archived_at is not None:
            raise ResourceConflictException("The cited source is no longer available.")
        if reference.chunk_id is not None:
            chunk = await self._session.get(DocumentRevisionChunkModel, reference.chunk_id)
            if (
                chunk is None
                or chunk.document_revision_id != revision.id
                or chunk.document_id != revision.document_id
            ):
                raise AuthorizationException()
            content = chunk.content
            title = f"{document.display_name} · chunk {chunk.chunk_index}"
        else:
            content = ""
            title = document.display_name
        return EvidencePayload(
            kind="document_chunk",
            space_id=revision.space_id,
            canonical_id=str(revision.document_id),
            revision_id=str(revision.id),
            chunk_id=str(reference.chunk_id) if reference.chunk_id else None,
            title=title,
            content=content,
            version=revision.version,
            superseded=document.current_revision_id != revision.id,
            citation_uri=(
                f"openknowledge://spaces/{revision.space_id}/documents/{revision.document_id}"
                f"/revisions/{revision.id}"
                + (f"/chunks/{reference.chunk_id}" if reference.chunk_id else "")
            ),
        )


def evidence_response(payload: EvidencePayload) -> dict:
    return {
        "kind": payload.kind,
        "space_id": payload.space_id,
        "canonical_id": payload.canonical_id,
        "revision_id": payload.revision_id,
        "chunk_id": payload.chunk_id,
        "title": payload.title,
        "content": payload.content,
        "version": payload.version,
        "superseded": payload.superseded,
        "citation_uri": payload.citation_uri,
    }
