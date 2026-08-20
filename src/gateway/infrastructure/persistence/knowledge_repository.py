

from __future__ import annotations

import logging
import math
from typing import Any, List, Optional
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

from src.gateway.application.ports.repositories import IKnowledgeRepository
from src.gateway.domain.canonical import RankedSearchResult
from src.gateway.domain.entities import (
    KnowledgeItem as DomainKnowledgeItem,
    KnowledgeRevision as DomainKnowledgeRevision,
    utc_now,
)
from src.gateway.domain.exceptions import AuthorizationException, ConcurrencyConflictException, ItemNotFoundException
from src.gateway.infrastructure.database import get_session_factory
from src.gateway.infrastructure.persistence.principal_context import get_bound_principal
from src.gateway.infrastructure.persistence.models import (
    KnowledgeItem as ORMKnowledgeItem,
    KnowledgeRevision as ORMKnowledgeRevision,
    Workspace as ORMWorkspace,
)
from src.gateway.infrastructure.persistence.ingestion_models import ProvenanceLinkModel, RetrievalUnitModel

class KnowledgeRepository(IKnowledgeRepository):

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()

    @staticmethod
    def _require_scope(scope: str) -> None:
        principal = get_bound_principal()
        if principal is not None and "*" not in principal.scopes and scope not in principal.scopes:
            raise AuthorizationException()

    def _to_domain_revision(self, orm_rev: ORMKnowledgeRevision) -> DomainKnowledgeRevision:

        created_at = orm_rev.__dict__.get("created_at") or getattr(orm_rev, "created_at", None) or utc_now()
        emb = list(orm_rev.embedding) if getattr(orm_rev, "embedding", None) is not None else None
        return DomainKnowledgeRevision(
            id=orm_rev.id,
            item_id=orm_rev.item_id,
            version=orm_rev.version,
            title=orm_rev.title,
            content=orm_rev.content,
            content_hash=orm_rev.content_hash,
            tags=list(orm_rev.tags or []),
            change_summary=orm_rev.change_summary,
            author=orm_rev.author,
            embedding=emb,
            created_at=created_at,
        )

    def _to_domain_item(
        self,
        orm_item: ORMKnowledgeItem,
        orm_rev: Optional[ORMKnowledgeRevision] = None,
    ) -> DomainKnowledgeItem:

        domain_rev = self._to_domain_revision(orm_rev) if orm_rev is not None else None
        version = domain_rev.version if domain_rev is not None else 1
        created_at = orm_item.__dict__.get("created_at") or getattr(orm_item, "created_at", None) or utc_now()
        updated_at = orm_item.__dict__.get("updated_at") or getattr(orm_item, "updated_at", None) or utc_now()
        content = domain_rev.content if domain_rev is not None else orm_item.content

        return DomainKnowledgeItem(
            id=orm_item.id,
            workspace_id=orm_item.workspace_id,
            is_global=orm_item.is_global,
            version=version,
            title=orm_item.title,
            content=content,
            tags=list(orm_item.tags or []),
            is_deleted=orm_item.is_deleted,
            created_at=created_at,
            updated_at=updated_at,
            current_revision=domain_rev,
        )

    async def create_item(
        self,
        item: DomainKnowledgeItem,
        initial_revision: DomainKnowledgeRevision,
    ) -> DomainKnowledgeItem:

        self._require_scope("knowledge:write")
        now = utc_now()
        async with self.session_factory() as session:
            async with session.begin():

                ws_res = await session.execute(
                    select(ORMWorkspace).where(ORMWorkspace.id == item.workspace_id)
                )
                if ws_res.scalar_one_or_none() is None:
                    raise ItemNotFoundException(
                        f"Workspace '{item.workspace_id}' not found."
                    )

                orm_item = ORMKnowledgeItem(
                    id=item.id,
                    workspace_id=item.workspace_id,
                    title=item.title,
                    content=item.content or initial_revision.content,
                    current_revision_id=None,
                    tags=list(item.tags),
                    is_global=item.is_global,
                    is_deleted=item.is_deleted,
                    archived_at=item.updated_at if item.is_deleted else None,
                    revision=max(item.version, 1),
                    created_at=item.created_at or now,
                    updated_at=item.updated_at or now,
                )
                session.add(orm_item)
                await session.flush()

                orm_rev = ORMKnowledgeRevision(
                    id=initial_revision.id,
                    item_id=item.id,
                    space_id=item.workspace_id,
                    version=initial_revision.version,
                    title=initial_revision.title or item.title,
                    content_hash=initial_revision.content_hash
                    or DomainKnowledgeRevision.compute_hash(initial_revision.content),
                    content=initial_revision.content,
                    tags=list(initial_revision.tags or item.tags),
                    change_summary=initial_revision.change_summary,
                    embedding=initial_revision.embedding,
                    author=initial_revision.author or "system",
                    author_member_id=self._actor_member_id(),
                    created_at=initial_revision.created_at or now,
                )
                session.add(orm_rev)
                await session.flush()

                session.add(
                    ProvenanceLinkModel(
                        space_id=item.workspace_id,
                        knowledge_revision_id=orm_rev.id,
                        source_type=self._provenance_type(initial_revision.provenance_type),
                        actor_member_id=self._actor_member_id(),
                        source_metadata=dict(initial_revision.provenance_metadata),
                    )
                )

                orm_item.current_revision_id = orm_rev.id
                await session.flush()

                await session.refresh(orm_item, attribute_names=["updated_at"])

                return self._to_domain_item(orm_item, orm_rev)

    async def get_item_by_id(
        self,
        item_id: UUID,
        version: Optional[int] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[DomainKnowledgeItem]:

        self._require_scope("knowledge:read")
        async with self.session_factory() as session:
            if version is not None:
                stmt = (
                    select(ORMKnowledgeItem, ORMKnowledgeRevision)
                    .join(ORMKnowledgeRevision, (ORMKnowledgeRevision.item_id == ORMKnowledgeItem.id) & (ORMKnowledgeRevision.version == version))
                    .where(ORMKnowledgeItem.id == item_id)
                )
                if workspace_id is not None:
                    stmt = stmt.where(
                        ORMKnowledgeItem.workspace_id == workspace_id
                    )
                res = await session.execute(stmt)
                row = res.first()
                if row is None:
                    return None
                orm_item, orm_rev = row
                if orm_item.is_deleted:
                    return None
                return self._to_domain_item(orm_item, orm_rev)
            else:
                stmt = (
                    select(ORMKnowledgeItem, ORMKnowledgeRevision)
                    .outerjoin(ORMKnowledgeRevision, ORMKnowledgeRevision.id == ORMKnowledgeItem.current_revision_id)
                    .where(
                        ORMKnowledgeItem.id == item_id,
                        ORMKnowledgeItem.is_deleted == False,
                    )
                )
                if workspace_id is not None:
                    stmt = stmt.where(
                        ORMKnowledgeItem.workspace_id == workspace_id
                    )
                res = await session.execute(stmt)
                row = res.first()
                if row is None:
                    return None
                orm_item, orm_rev = row
                return self._to_domain_item(orm_item, orm_rev)

    async def update_item_occ(
        self,
        item_id: UUID,
        expected_version: int,
        new_revision: DomainKnowledgeRevision,
        title: Optional[str] = None,
        tags: Optional[List[str]] = None,
        is_global: Optional[bool] = None,
        workspace_id: Optional[str] = None,
    ) -> DomainKnowledgeItem:

        self._require_scope("knowledge:write")
        now = utc_now()
        async with self.session_factory() as session:
            async with session.begin():
                stmt = (
                    select(ORMKnowledgeItem, ORMKnowledgeRevision)
                    .outerjoin(ORMKnowledgeRevision, ORMKnowledgeRevision.id == ORMKnowledgeItem.current_revision_id)
                    .where(ORMKnowledgeItem.id == item_id)
                )
                if workspace_id is not None:
                    stmt = stmt.where(
                        ORMKnowledgeItem.workspace_id == workspace_id
                    )
                res = await session.execute(stmt)
                row = res.first()

                if row is None:
                    raise ItemNotFoundException(f"Knowledge item '{item_id}' not found.")

                orm_item, current_rev = row

                if orm_item.is_deleted:
                    raise ItemNotFoundException(f"Knowledge item '{item_id}' not found.")

                current_version = current_rev.version if current_rev is not None else 1

                if expected_version != current_version:
                    raise ConcurrencyConflictException(
                        message_or_item_id=str(item_id),
                        expected_version=expected_version,
                        actual_version=current_version,
                    )

                new_version = current_version + 1
                content_hash = (
                    new_revision.content_hash
                    if new_revision.content_hash
                    else DomainKnowledgeRevision.compute_hash(new_revision.content)
                )

                orm_rev = ORMKnowledgeRevision(
                    id=new_revision.id if new_revision.id != (current_rev.id if current_rev else None) else uuid4(),
                    item_id=item_id,
                    space_id=orm_item.workspace_id,
                    version=new_version,
                    title=title if title is not None else orm_item.title,
                    content_hash=content_hash,
                    content=new_revision.content,
                    tags=list(tags if tags is not None else new_revision.tags or orm_item.tags),
                    change_summary=new_revision.change_summary,
                    embedding=new_revision.embedding,
                    author=new_revision.author or "system",
                    author_member_id=self._actor_member_id(),
                    created_at=new_revision.created_at or now,
                )

                session.add(orm_rev)
                try:
                    await session.flush()
                except IntegrityError as exc:

                    raise ConcurrencyConflictException(
                        message_or_item_id=str(item_id),
                        expected_version=expected_version,
                        actual_version=new_version,
                        message=(
                            f"Version conflict on item {item_id}: concurrent update "
                            f"detected while writing version {new_version}. "
                            "Please re-read and retry."
                        ),
                    ) from exc

                values = {
                    "content": new_revision.content,
                    "current_revision_id": orm_rev.id,
                    "updated_at": now,
                    "revision": ORMKnowledgeItem.revision + 1,
                    "title": title if title is not None else orm_item.title,
                    "tags": list(tags if tags is not None else new_revision.tags or orm_item.tags),
                    "is_global": is_global if is_global is not None else orm_item.is_global,
                }
                changed = await session.execute(
                    update(ORMKnowledgeItem)
                    .where(
                        ORMKnowledgeItem.id == item_id,
                        ORMKnowledgeItem.current_revision_id == current_rev.id,
                        ORMKnowledgeItem.is_deleted.is_(False),
                    )
                    .values(**values)
                    .returning(ORMKnowledgeItem.id)
                    .execution_options(synchronize_session=False)
                )
                if changed.scalar_one_or_none() is None:
                    raise ConcurrencyConflictException(
                        message_or_item_id=str(item_id),
                        expected_version=expected_version,
                        actual_version=new_version,
                    )
                session.add(
                    ProvenanceLinkModel(
                        space_id=orm_item.workspace_id,
                        knowledge_revision_id=orm_rev.id,
                        source_type=self._provenance_type(new_revision.provenance_type),
                        actor_member_id=self._actor_member_id(),
                        source_metadata=dict(new_revision.provenance_metadata),
                    )
                )
                await session.flush()
                await session.refresh(orm_item)
                await session.refresh(orm_item, attribute_names=["updated_at"])
                return self._to_domain_item(orm_item, orm_rev)

    async def soft_delete_item(
        self,
        item_id: UUID,
        expected_version: Optional[int] = None,
        workspace_id: Optional[str] = None,
    ) -> bool:

        self._require_scope("knowledge:write")
        async with self.session_factory() as session:
            async with session.begin():
                stmt = (
                    select(ORMKnowledgeItem, ORMKnowledgeRevision)
                    .outerjoin(ORMKnowledgeRevision, ORMKnowledgeRevision.id == ORMKnowledgeItem.current_revision_id)
                    .where(ORMKnowledgeItem.id == item_id)
                )
                if workspace_id is not None:
                    stmt = stmt.where(
                        ORMKnowledgeItem.workspace_id == workspace_id
                    )
                res = await session.execute(stmt)
                row = res.first()

                if row is None:
                    return False

                orm_item, current_rev = row

                if expected_version is not None:
                    current_version = current_rev.version if current_rev is not None else 1
                    if expected_version != current_version:
                        raise ConcurrencyConflictException(
                            message_or_item_id=str(item_id),
                            expected_version=expected_version,
                            actual_version=current_version,
                        )

                now = utc_now()
                current_revision_id = current_rev.id if current_rev is not None else None
                deleted = await session.execute(
                    update(ORMKnowledgeItem)
                    .where(
                        ORMKnowledgeItem.id == item_id,
                        ORMKnowledgeItem.current_revision_id == current_revision_id,
                        ORMKnowledgeItem.is_deleted.is_(False),
                    )
                    .values(
                        is_deleted=True,
                        archived_at=now,
                        revision=ORMKnowledgeItem.revision + 1,
                        updated_at=now,
                    )
                    .returning(ORMKnowledgeItem.id)
                    .execution_options(synchronize_session=False)
                )
                if deleted.scalar_one_or_none() is None:
                    if expected_version is None:
                        return False
                    raise ConcurrencyConflictException(
                        message_or_item_id=str(item_id),
                        expected_version=expected_version,
                        actual_version=current_version,
                    )
                if current_revision_id is not None:
                    await session.execute(
                        update(RetrievalUnitModel)
                        .where(
                            RetrievalUnitModel.knowledge_revision_id == current_revision_id,
                            RetrievalUnitModel.active.is_(True),
                        )
                        .values(active=False, deactivated_at=now)
                    )
                return True

    @staticmethod
    def _actor_member_id() -> UUID | None:
        principal = get_bound_principal()
        if principal is None:
            return None
        try:
            return UUID(principal.subject_id)
        except ValueError:
            return None

    @staticmethod
    def _provenance_type(value: str) -> str:
        return value if value in {"manual", "ai_action"} else "manual"

    async def list_revisions(self, item_id: UUID) -> List[DomainKnowledgeRevision]:

        self._require_scope("knowledge:read")
        async with self.session_factory() as session:
            stmt = (
                select(ORMKnowledgeRevision)
                .where(ORMKnowledgeRevision.item_id == item_id)
                .order_by(ORMKnowledgeRevision.version.asc())
            )
            res = await session.execute(stmt)
            orm_revisions = res.scalars().all()
            return [self._to_domain_revision(r) for r in orm_revisions]

    async def search_fts(
        self,
        query: str,
        workspace_id: str,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

        self._require_scope("knowledge:read")
        if not query or not query.strip():
            return []

        async with self.session_factory() as session:
            stmt = (
                select(ORMKnowledgeItem, ORMKnowledgeRevision)
                .outerjoin(ORMKnowledgeRevision, ORMKnowledgeRevision.id == ORMKnowledgeItem.current_revision_id)
                .where(
                    ORMKnowledgeItem.is_deleted == False,
                    ORMKnowledgeItem.workspace_id == workspace_id,
                )
            )
            res = await session.execute(stmt)
            rows = res.all()

            query_terms = [t.lower() for t in query.split() if t.strip()]
            scored_items: List[tuple[float, ORMKnowledgeItem, Optional[ORMKnowledgeRevision]]] = []

            for item, rev in rows:
                title_lower = (item.title or "").lower()
                content_lower = (item.content or "").lower()
                score = 0.0
                matched = False
                for term in query_terms:
                    if term in title_lower:
                        score += 3.0
                        matched = True
                    if term in content_lower:
                        score += 1.0
                        matched = True

                if matched or not query_terms:
                    scored_items.append((score, item, rev))

            scored_items.sort(key=lambda x: x[0], reverse=True)
            top_items = scored_items[:limit]

            results: List[RankedSearchResult] = []
            for rank_idx, (score, item, rev) in enumerate(top_items, start=1):
                version = rev.version if rev is not None else 1
                results.append(
                    RankedSearchResult(
                        id=str(item.id),
                        source_type="knowledge",
                        title=item.title,
                        content=item.content,
                        metadata={
                            "item_id": str(item.id),
                            "workspace_id": item.workspace_id,
                            "is_global": item.is_global,
                            "version": version,
                        },
                        rank=rank_idx,
                        raw_score=score,
                        workspace_id=item.workspace_id,
                        is_global=item.is_global,
                        version=version,
                    )
                )
            return results

    async def search_vector(
        self,
        query_vector: List[float],
        workspace_id: str,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

        self._require_scope("knowledge:read")
        if not query_vector:
            return []

        async with self.session_factory() as session:
            bind = session.bind or getattr(session, "sync_session", None)
            dialect_name = (
                bind.engine.dialect.name
                if bind and hasattr(bind, "engine")
                else (bind.dialect.name if bind and hasattr(bind, "dialect") else "postgresql")
            )

            if "sqlite" not in dialect_name.lower():
                try:
                    distance_col = ORMKnowledgeRevision.embedding.cosine_distance(query_vector).label("distance")
                    stmt = (
                        select(ORMKnowledgeItem, ORMKnowledgeRevision, distance_col)
                        .join(ORMKnowledgeRevision, ORMKnowledgeRevision.id == ORMKnowledgeItem.current_revision_id)
                        .where(
                            ORMKnowledgeItem.is_deleted == False,
                            ORMKnowledgeItem.workspace_id == workspace_id,
                            ORMKnowledgeRevision.embedding.isnot(None),
                        )
                        .order_by(distance_col.asc())
                        .limit(limit)
                    )
                    res = await session.execute(stmt)
                    pg_rows = res.all()
                    results: List[RankedSearchResult] = []
                    for rank_idx, (item, rev, dist) in enumerate(pg_rows, start=1):
                        version = rev.version if rev is not None else 1
                        score = max(0.0, 1.0 - (float(dist) if dist is not None else 1.0))
                        results.append(
                            RankedSearchResult(
                                id=str(item.id),
                                source_type="knowledge",
                                title=item.title,
                                content=item.content,
                                metadata={
                                    "item_id": str(item.id),
                                    "workspace_id": item.workspace_id,
                                    "is_global": item.is_global,
                                    "version": version,
                                },
                                rank=rank_idx,
                                raw_score=score,
                                workspace_id=item.workspace_id,
                                is_global=item.is_global,
                                version=version,
                            )
                        )
                    return results
                except Exception as exc:
                    logger.debug(
                        "Knowledge vector pushdown fallback",
                        extra={"exception_class": type(exc).__name__},
                    )

            stmt = (
                select(ORMKnowledgeItem, ORMKnowledgeRevision)
                .join(ORMKnowledgeRevision, ORMKnowledgeRevision.id == ORMKnowledgeItem.current_revision_id)
                .where(
                    ORMKnowledgeItem.is_deleted == False,
                    ORMKnowledgeItem.workspace_id == workspace_id,
                    ORMKnowledgeRevision.embedding.isnot(None),
                )
            )
            res = await session.execute(stmt)
            rows = res.all()

            def cosine_similarity(v1: List[float], v2: List[float]) -> float:
                if not v1 or not v2 or len(v1) != len(v2):
                    return 0.0
                dot = sum(a * b for a, b in zip(v1, v2))
                norm1 = math.sqrt(sum(a * a for a in v1))
                norm2 = math.sqrt(sum(b * b for b in v2))
                if norm1 == 0.0 or norm2 == 0.0:
                    return 0.0
                return dot / (norm1 * norm2)

            scored: List[tuple[float, ORMKnowledgeItem, ORMKnowledgeRevision]] = []
            for item, rev in rows:
                if rev.embedding is not None:
                    emb = list(rev.embedding)
                    sim = cosine_similarity(query_vector, emb)
                    scored.append((sim, item, rev))

            scored.sort(key=lambda x: x[0], reverse=True)
            top = scored[:limit]

            results = []
            for rank_idx, (score, item, rev) in enumerate(top, start=1):
                version = rev.version if rev is not None else 1
                results.append(
                    RankedSearchResult(
                        id=str(item.id),
                        source_type="knowledge",
                        title=item.title,
                        content=item.content,
                        metadata={
                            "item_id": str(item.id),
                            "workspace_id": item.workspace_id,
                            "is_global": item.is_global,
                            "version": version,
                        },
                        rank=rank_idx,
                        raw_score=score,
                        workspace_id=item.workspace_id,
                        is_global=item.is_global,
                        version=version,
                    )
                )
            return results
