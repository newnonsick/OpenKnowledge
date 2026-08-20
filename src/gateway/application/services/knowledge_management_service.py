from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.application.services.audit_service import AuditService
from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.domain.authorization import Action
from src.gateway.domain.entities import KnowledgeItem as DomainKnowledgeItem, KnowledgeRevision as DomainKnowledgeRevision
from src.gateway.domain.exceptions import AuthorizationException, ConcurrencyConflictException
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.persistence.audit_repository import AuditRepository
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, ProvenanceLinkModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import KnowledgeItem, KnowledgeRevision


class KnowledgeManagementService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(AuditRepository(session))

    async def get(self, item_id: UUID) -> DomainKnowledgeItem | None:
        row = (
            await self._session.execute(
                select(KnowledgeItem, KnowledgeRevision)
                .outerjoin(KnowledgeRevision, KnowledgeRevision.id == KnowledgeItem.current_revision_id)
                .where(KnowledgeItem.id == item_id, KnowledgeItem.is_deleted.is_(False))
            )
        ).one_or_none()
        if row is None or row[1] is None:
            return None
        return self._domain(row[0], row[1])

    async def create(
        self,
        principal: Principal,
        *,
        space_id: str,
        title: str,
        content: str,
        tags: list[str],
        request_id: str,
    ) -> DomainKnowledgeItem:
        await AuthorizationService(self._session).authorize_space(principal, space_id, Action.CONTENT_WRITE)
        now = datetime.now(timezone.utc)
        item_id = uuid4()
        revision_id = uuid4()
        item = KnowledgeItem(
            id=item_id,
            workspace_id=space_id,
            title=title,
            content=content,
            tags=tags,
            current_revision_id=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(item)
        await self._session.flush()
        revision = KnowledgeRevision(
            id=revision_id,
            item_id=item_id,
            space_id=space_id,
            version=1,
            title=title,
            content_hash=DomainKnowledgeRevision.compute_hash(content),
            content=content,
            tags=tags,
            author=principal.subject_id,
            author_member_id=self._member_id(principal),
            created_at=now,
        )
        self._session.add(revision)
        await self._session.flush()
        item.current_revision_id = revision_id
        self._session.add(
            ProvenanceLinkModel(
                space_id=space_id,
                knowledge_revision_id=revision_id,
                source_type="manual",
                actor_member_id=self._member_id(principal),
            )
        )
        await self._activate_projection(item, revision, now)
        self._audit.record(
            actor_member_id=self._member_id(principal),
            actor_kind=principal.kind.value,
            request_id=request_id,
            action="knowledge.create",
            resource_type="knowledge_item",
            resource_id=str(item_id),
            details={"space_id": space_id},
        )
        await self._session.flush()
        await self._session.refresh(item)
        await self._session.refresh(revision)
        return self._domain(item, revision)

    async def update(
        self,
        principal: Principal,
        item_id: UUID,
        *,
        expected_version: int,
        title: str,
        content: str,
        tags: list[str],
        change_summary: str | None,
        request_id: str,
    ) -> DomainKnowledgeItem:
        row = (
            await self._session.execute(
                select(KnowledgeItem, KnowledgeRevision)
                .join(KnowledgeRevision, KnowledgeRevision.id == KnowledgeItem.current_revision_id)
                .where(KnowledgeItem.id == item_id, KnowledgeItem.is_deleted.is_(False))
                .with_for_update(of=KnowledgeItem)
            )
        ).one_or_none()
        if row is None:
            raise AuthorizationException()
        item, current = row
        await AuthorizationService(self._session).authorize_space(principal, item.workspace_id, Action.CONTENT_WRITE)
        if current.version != expected_version:
            raise ConcurrencyConflictException(
                message_or_item_id=str(item_id),
                expected_version=expected_version,
                actual_version=current.version,
            )
        now = datetime.now(timezone.utc)
        revision = KnowledgeRevision(
            id=uuid4(),
            item_id=item.id,
            space_id=item.workspace_id,
            version=current.version + 1,
            title=title,
            content_hash=DomainKnowledgeRevision.compute_hash(content),
            content=content,
            tags=tags,
            change_summary=change_summary,
            author=principal.subject_id,
            author_member_id=self._member_id(principal),
            created_at=now,
        )
        self._session.add(revision)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ConcurrencyConflictException("A concurrent knowledge update was detected.") from exc
        item.title = title
        item.content = content
        item.tags = tags
        item.current_revision_id = revision.id
        item.revision += 1
        item.updated_at = now
        self._session.add(
            ProvenanceLinkModel(
                space_id=item.workspace_id,
                knowledge_revision_id=revision.id,
                source_type="manual",
                actor_member_id=self._member_id(principal),
            )
        )
        await self._activate_projection(item, revision, now)
        self._audit.record(
            actor_member_id=self._member_id(principal),
            actor_kind=principal.kind.value,
            request_id=request_id,
            action="knowledge.update",
            resource_type="knowledge_item",
            resource_id=str(item.id),
            details={"space_id": item.workspace_id, "version": revision.version},
        )
        await self._session.flush()
        await self._session.refresh(item)
        await self._session.refresh(revision)
        return self._domain(item, revision)

    async def delete(
        self,
        principal: Principal,
        item_id: UUID,
        *,
        expected_version: int,
        request_id: str,
    ) -> None:
        row = (
            await self._session.execute(
                select(KnowledgeItem, KnowledgeRevision)
                .join(KnowledgeRevision, KnowledgeRevision.id == KnowledgeItem.current_revision_id)
                .where(KnowledgeItem.id == item_id, KnowledgeItem.is_deleted.is_(False))
                .with_for_update(of=KnowledgeItem)
            )
        ).one_or_none()
        if row is None:
            raise AuthorizationException()
        item, revision = row
        await AuthorizationService(self._session).authorize_space(principal, item.workspace_id, Action.CONTENT_WRITE)
        if revision.version != expected_version:
            raise ConcurrencyConflictException(
                message_or_item_id=str(item_id),
                expected_version=expected_version,
                actual_version=revision.version,
            )
        now = datetime.now(timezone.utc)
        item.is_deleted = True
        item.archived_at = now
        item.updated_at = now
        item.revision += 1
        await self._session.execute(
            update(RetrievalUnitModel)
            .where(
                RetrievalUnitModel.knowledge_revision_id == revision.id,
                RetrievalUnitModel.active.is_(True),
            )
            .values(active=False, deactivated_at=now)
        )
        self._audit.record(
            actor_member_id=self._member_id(principal),
            actor_kind=principal.kind.value,
            request_id=request_id,
            action="knowledge.delete",
            resource_type="knowledge_item",
            resource_id=str(item.id),
            details={"space_id": item.workspace_id, "version": revision.version},
        )

    async def _activate_projection(self, item: KnowledgeItem, revision: KnowledgeRevision, now: datetime) -> None:
        generation_id = await self._session.scalar(
            select(EmbeddingGenerationModel.id)
            .where(
                EmbeddingGenerationModel.purpose == "retrieval",
                EmbeddingGenerationModel.status == "active",
            )
            .order_by(EmbeddingGenerationModel.activated_at.desc().nullslast(), EmbeddingGenerationModel.created_at.desc())
            .limit(1)
        )
        if generation_id is None:
            return
        revision_ids = select(KnowledgeRevision.id).where(KnowledgeRevision.item_id == item.id)
        await self._session.execute(
            update(RetrievalUnitModel)
            .where(RetrievalUnitModel.knowledge_revision_id.in_(revision_ids), RetrievalUnitModel.active.is_(True))
            .values(active=False, deactivated_at=now)
            .execution_options(synchronize_session=False)
        )
        self._session.add(
            RetrievalUnitModel(
                space_id=item.workspace_id,
                source_type="knowledge_revision",
                knowledge_revision_id=revision.id,
                embedding_generation_id=generation_id,
                title=revision.title or item.title,
                content=revision.content,
                language=self._language(revision.content),
                source_metadata={
                    "knowledge_item_id": str(item.id),
                    "knowledge_revision_id": str(revision.id),
                    "version": revision.version,
                    "tags": list(revision.tags),
                },
                embedding=None,
                active=True,
            )
        )

    @staticmethod
    def _member_id(principal: Principal) -> UUID:
        try:
            return UUID(principal.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc

    @staticmethod
    def _language(content: str) -> str | None:
        thai = any("\u0e00" <= character <= "\u0e7f" for character in content)
        latin = any(character.isascii() and character.isalpha() for character in content)
        if thai and latin:
            return "mixed"
        if thai:
            return "th"
        if latin:
            return "en"
        return None

    @staticmethod
    def _domain(item: KnowledgeItem, revision: KnowledgeRevision) -> DomainKnowledgeItem:
        domain_revision = DomainKnowledgeRevision(
            id=revision.id,
            item_id=item.id,
            version=revision.version,
            title=revision.title,
            content=revision.content,
            content_hash=revision.content_hash,
            tags=list(revision.tags),
            change_summary=revision.change_summary,
            author=revision.author,
            created_at=revision.created_at,
        )
        return DomainKnowledgeItem(
            id=item.id,
            workspace_id=item.workspace_id,
            version=revision.version,
            title=item.title,
            content=revision.content,
            tags=list(item.tags),
            is_deleted=item.is_deleted,
            created_at=item.created_at,
            updated_at=item.updated_at,
            current_revision=domain_revision,
        )
