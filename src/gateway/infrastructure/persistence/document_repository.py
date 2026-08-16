

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.gateway.application.ports.repositories import IDocumentRepository
from src.gateway.domain.canonical import RankedSearchResult
from src.gateway.domain.entities import (
    DocumentChunk as DomainDocumentChunk,
    DocumentFile as DomainDocumentFile,
    utc_now,
)
from src.gateway.infrastructure.database import get_session_factory
from src.gateway.infrastructure.persistence.models import (
    DocumentChunk as ORMDocumentChunk,
    DocumentFile as ORMDocumentFile,
    Workspace as ORMWorkspace,
)

logger = logging.getLogger(__name__)

class DocumentRepository(IDocumentRepository):

    def __init__(
        self,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()

    def _to_domain_file(
        self,
        orm_doc: ORMDocumentFile,
        total_chunks: int = 0,
    ) -> DomainDocumentFile:

        return DomainDocumentFile(
            id=orm_doc.id,
            workspace_id=orm_doc.workspace_id,
            is_global=orm_doc.is_global,
            filename=orm_doc.filename,
            file_path=orm_doc.file_path,
            file_size=orm_doc.file_size,
            file_size_bytes=orm_doc.file_size,
            mime_type=orm_doc.mime_type,
            total_chunks=total_chunks,
            is_deleted=False,
            created_at=orm_doc.created_at,
        )

    def _to_domain_chunk(self, orm_chunk: ORMDocumentChunk) -> DomainDocumentChunk:

        meta = dict(orm_chunk.metadata_) if orm_chunk.metadata_ else {}
        content_hash = meta.get("content_hash", "")
        if not content_hash and orm_chunk.content:
            content_hash = DomainDocumentChunk(
                document_id=orm_chunk.document_id,
                content=orm_chunk.content,
            ).content_hash

        emb = list(orm_chunk.embedding) if orm_chunk.embedding is not None else None

        return DomainDocumentChunk(
            id=orm_chunk.id,
            document_id=orm_chunk.document_id,
            workspace_id=orm_chunk.workspace_id,
            is_global=orm_chunk.is_global,
            chunk_index=orm_chunk.chunk_index,
            content=orm_chunk.content,
            content_hash=content_hash,
            metadata=meta,
            embedding=emb,
            created_at=getattr(orm_chunk, "created_at", None) or utc_now(),
        )

    async def save_document(self, document: DomainDocumentFile) -> DomainDocumentFile:

        async with self.session_factory() as session:
            async with session.begin():

                ws_res = await session.execute(
                    select(ORMWorkspace).where(ORMWorkspace.id == document.workspace_id)
                )
                if ws_res.scalar_one_or_none() is None:
                    workspace = ORMWorkspace(
                        id=document.workspace_id,
                        name=f"Workspace {document.workspace_id}",
                    )
                    session.add(workspace)
                    await session.flush()

                stmt = select(ORMDocumentFile).where(ORMDocumentFile.id == document.id)
                res = await session.execute(stmt)
                existing = res.scalar_one_or_none()

                file_size = document.file_size if document.file_size is not None else document.file_size_bytes

                if existing is not None:
                    existing.filename = document.filename
                    existing.file_path = document.file_path
                    existing.file_size = file_size
                    existing.mime_type = document.mime_type
                    existing.is_global = document.is_global
                    await session.flush()
                    return self._to_domain_file(existing, total_chunks=document.total_chunks)
                else:
                    orm_doc = ORMDocumentFile(
                        id=document.id,
                        workspace_id=document.workspace_id,
                        filename=document.filename,
                        file_path=document.file_path,
                        file_size=file_size,
                        mime_type=document.mime_type,
                        is_global=document.is_global,
                        created_at=document.created_at,
                    )
                    session.add(orm_doc)
                    await session.flush()
                    return self._to_domain_file(orm_doc, total_chunks=document.total_chunks)

    async def get_by_hash(self, workspace_id: str, content_hash: str) -> Optional[DomainDocumentFile]:

        async with self.session_factory() as session:

            stmt = (
                select(ORMDocumentFile)
                .join(ORMDocumentChunk, ORMDocumentChunk.document_id == ORMDocumentFile.id)
                .where(
                    or_(
                        ORMDocumentFile.workspace_id == workspace_id,
                        ORMDocumentFile.is_global == True,
                    ),
                    ORMDocumentChunk.metadata_["content_hash"].astext == content_hash,
                )
            )
            res = await session.execute(stmt)
            orm_doc = res.scalar_one_or_none()
            if orm_doc is not None:
                return self._to_domain_file(orm_doc)
            return None

    async def get_by_id(self, document_id: UUID) -> Optional[DomainDocumentFile]:

        async with self.session_factory() as session:
            stmt = select(ORMDocumentFile).where(ORMDocumentFile.id == document_id)
            res = await session.execute(stmt)
            orm_doc = res.scalar_one_or_none()
            if orm_doc is None:
                return None

            count_stmt = select(func.count()).select_from(ORMDocumentChunk).where(
                ORMDocumentChunk.document_id == document_id
            )
            count_res = await session.execute(count_stmt)
            total_chunks = count_res.scalar() or 0

            return self._to_domain_file(orm_doc, total_chunks=total_chunks)

    async def save_chunks_batch(self, chunks: List[DomainDocumentChunk]) -> int:

        if not chunks:
            return 0

        async with self.session_factory() as session:
            async with session.begin():
                for c in chunks:
                    meta = dict(c.metadata) if c.metadata else {}
                    if c.content_hash and "content_hash" not in meta:
                        meta["content_hash"] = c.content_hash

                    orm_chunk = ORMDocumentChunk(
                        id=c.id,
                        document_id=c.document_id,
                        workspace_id=c.workspace_id,
                        is_global=c.is_global,
                        chunk_index=c.chunk_index,
                        content=c.content,
                        embedding=c.embedding,
                        metadata_=meta,
                    )
                    session.add(orm_chunk)

                await session.flush()
                return len(chunks)

    async def get_chunks_by_document(self, document_id: UUID) -> List[DomainDocumentChunk]:

        async with self.session_factory() as session:
            stmt = (
                select(ORMDocumentChunk)
                .where(ORMDocumentChunk.document_id == document_id)
                .order_by(ORMDocumentChunk.chunk_index.asc())
            )
            res = await session.execute(stmt)
            orm_chunks = res.scalars().all()
            return [self._to_domain_chunk(c) for c in orm_chunks]

    async def search_chunks_fts(
        self,
        query: str,
        workspace_id: str,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

        if not query or not query.strip():
            return []

        async with self.session_factory() as session:
            stmt = (
                select(ORMDocumentChunk, ORMDocumentFile)
                .join(ORMDocumentFile, ORMDocumentFile.id == ORMDocumentChunk.document_id)
                .where(
                    or_(
                        ORMDocumentChunk.workspace_id == workspace_id,
                        ORMDocumentChunk.is_global == True,
                    )
                )
            )
            res = await session.execute(stmt)
            rows = res.all()

            query_terms = [t.lower() for t in query.split() if t.strip()]
            scored_chunks: List[tuple[float, ORMDocumentChunk, ORMDocumentFile]] = []

            for chunk, doc in rows:
                content_lower = (chunk.content or "").lower()
                filename_lower = (doc.filename or "").lower()
                score = 0.0
                matched = False
                for term in query_terms:
                    if term in content_lower:
                        score += 1.0
                        matched = True
                    if term in filename_lower:
                        score += 2.0
                        matched = True

                if matched or not query_terms:
                    scored_chunks.append((score, chunk, doc))

            scored_chunks.sort(key=lambda x: x[0], reverse=True)
            top_chunks = scored_chunks[:limit]

            results: List[RankedSearchResult] = []
            for rank_idx, (score, chunk, doc) in enumerate(top_chunks, start=1):
                results.append(
                    RankedSearchResult(
                        id=str(chunk.id),
                        source_type="document_chunk",
                        title=doc.filename,
                        content=chunk.content,
                        metadata={
                            "document_id": str(doc.id),
                            "chunk_index": chunk.chunk_index,
                            "filename": doc.filename,
                            "workspace_id": chunk.workspace_id,
                            **(chunk.metadata_ or {}),
                        },
                        rank=rank_idx,
                        raw_score=score,
                        workspace_id=chunk.workspace_id,
                        is_global=chunk.is_global,
                    )
                )
            return results

    async def search_chunks_vector(
        self,
        query_vector: List[float],
        workspace_id: str,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

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
                    distance_col = ORMDocumentChunk.embedding.cosine_distance(query_vector).label("distance")
                    stmt = (
                        select(ORMDocumentChunk, ORMDocumentFile, distance_col)
                        .join(ORMDocumentFile, ORMDocumentFile.id == ORMDocumentChunk.document_id)
                        .where(
                            or_(
                                ORMDocumentChunk.workspace_id == workspace_id,
                                ORMDocumentChunk.is_global == True,
                            ),
                            ORMDocumentChunk.embedding.isnot(None),
                        )
                        .order_by(distance_col.asc())
                        .limit(limit)
                    )
                    res = await session.execute(stmt)
                    pg_rows = res.all()
                    results: List[RankedSearchResult] = []
                    for rank_idx, (chunk, doc, dist) in enumerate(pg_rows, start=1):
                        score = max(0.0, 1.0 - (float(dist) if dist is not None else 1.0))
                        results.append(
                            RankedSearchResult(
                                id=str(chunk.id),
                                source_type="document_chunk",
                                title=doc.filename,
                                content=chunk.content,
                                metadata={
                                    "document_id": str(doc.id),
                                    "chunk_index": chunk.chunk_index,
                                    "filename": doc.filename,
                                    "workspace_id": chunk.workspace_id,
                                    **(chunk.metadata_ or {}),
                                },
                                rank=rank_idx,
                                raw_score=score,
                                workspace_id=chunk.workspace_id,
                                is_global=chunk.is_global,
                            )
                        )
                    return results
                except Exception as exc:
                    logger.debug(f"PostgreSQL native chunk vector pushdown fallback to in-memory: {exc}")

            stmt = (
                select(ORMDocumentChunk, ORMDocumentFile)
                .join(ORMDocumentFile, ORMDocumentFile.id == ORMDocumentChunk.document_id)
                .where(
                    or_(
                        ORMDocumentChunk.workspace_id == workspace_id,
                        ORMDocumentChunk.is_global == True,
                    ),
                    ORMDocumentChunk.embedding.isnot(None),
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

            scored: List[tuple[float, ORMDocumentChunk, ORMDocumentFile]] = []
            for chunk, doc in rows:
                if chunk.embedding is not None:
                    emb = list(chunk.embedding)
                    sim = cosine_similarity(query_vector, emb)
                    scored.append((sim, chunk, doc))

            scored.sort(key=lambda x: x[0], reverse=True)
            top = scored[:limit]

            results = []
            for rank_idx, (score, chunk, doc) in enumerate(top, start=1):
                results.append(
                    RankedSearchResult(
                        id=str(chunk.id),
                        source_type="document_chunk",
                        title=doc.filename,
                        content=chunk.content,
                        metadata={
                            "document_id": str(doc.id),
                            "chunk_index": chunk.chunk_index,
                            "filename": doc.filename,
                            "workspace_id": chunk.workspace_id,
                            **(chunk.metadata_ or {}),
                        },
                        rank=rank_idx,
                        raw_score=score,
                        workspace_id=chunk.workspace_id,
                        is_global=chunk.is_global,
                    )
                )
            return results
