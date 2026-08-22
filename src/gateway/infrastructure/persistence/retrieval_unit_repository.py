from __future__ import annotations

import math
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal
from src.gateway.domain.retrieval import RetrievalCandidate
from src.gateway.infrastructure.database import get_session_factory, principal_session
from src.gateway.infrastructure.persistence.models import EMBED_DIM


_AUTHORIZED_SOURCE = """
FROM retrieval_units ru
JOIN space_memberships sm
  ON sm.space_id = ru.space_id AND sm.member_id = :member_id
JOIN members m
  ON m.id = sm.member_id AND m.status = 'active'
JOIN workspaces w
  ON w.id = ru.space_id AND w.archived_at IS NULL
LEFT JOIN knowledge_revisions kr
  ON kr.id = ru.knowledge_revision_id AND kr.space_id = ru.space_id
LEFT JOIN document_revision_chunks drc
  ON drc.id = ru.document_revision_chunk_id AND drc.space_id = ru.space_id
LEFT JOIN document_revisions dr
  ON dr.id = drc.document_revision_id
 AND dr.document_id = drc.document_id
 AND dr.space_id = drc.space_id
"""


_CANDIDATE_COLUMNS = """
ru.id AS unit_id,
ru.source_type,
ru.space_id,
CASE WHEN ru.source_type = 'knowledge_revision' THEN kr.item_id ELSE drc.document_id END AS canonical_id,
CASE WHEN ru.source_type = 'knowledge_revision' THEN kr.id ELSE drc.document_revision_id END AS revision_id,
ru.title,
ru.content,
ru.embedding_generation_id,
ru.language,
drc.id AS chunk_id,
drc.chunk_index,
dr.original_filename AS source_filename,
dr.parser_version,
COALESCE(kr.version, dr.version) AS version,
ru.source_metadata
"""


_VECTOR_SEARCH_SQL = (
    "WITH nearest AS MATERIALIZED ("
    "SELECT ru.id, ru.embedding <=> CAST(:query_vector AS vector) AS distance "
    "FROM retrieval_units ru "
    "WHERE ru.active AND ru.embedding IS NOT NULL "
    "AND ru.embedding_generation_id = :generation_id "
    "AND ru.space_id = ANY(CAST(:space_ids AS text[])) "
    "AND (ru.embedding <=> CAST(:query_vector AS vector)) <= :maximum_distance "
    "AND EXISTS ("
    "SELECT 1 FROM space_memberships sm "
    "JOIN members m ON m.id = sm.member_id AND m.status = 'active' "
    "JOIN workspaces w ON w.id = sm.space_id AND w.archived_at IS NULL "
    "WHERE sm.space_id = ru.space_id AND sm.member_id = :member_id"
    ") ORDER BY ru.embedding <=> CAST(:query_vector AS vector) "
    "LIMIT :candidate_limit"
    "), ranked AS ("
    "SELECT "
    + _CANDIDATE_COLUMNS
    + ", 1.0 - nearest.distance AS vector_similarity "
    "FROM nearest JOIN retrieval_units ru ON ru.id = nearest.id "
    "LEFT JOIN knowledge_revisions kr "
    "ON kr.id = ru.knowledge_revision_id AND kr.space_id = ru.space_id "
    "LEFT JOIN document_revision_chunks drc "
    "ON drc.id = ru.document_revision_chunk_id AND drc.space_id = ru.space_id "
    "LEFT JOIN document_revisions dr "
    "ON dr.id = drc.document_revision_id "
    "AND dr.document_id = drc.document_id "
    "AND dr.space_id = drc.space_id"
    ") SELECT * FROM ranked "
    "WHERE canonical_id IS NOT NULL AND revision_id IS NOT NULL "
    "ORDER BY vector_similarity DESC, unit_id ASC LIMIT :limit"
)


class PostgresRetrievalUnitRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()

    async def active_generation(self, principal: Principal) -> UUID | None:
        async with principal_session(self._session_factory, principal) as session:
            return await session.scalar(
                text(
                    "SELECT id FROM embedding_generations "
                    "WHERE purpose = 'retrieval' AND status = 'active' "
                    "ORDER BY activated_at DESC NULLS LAST, created_at DESC LIMIT 1"
                )
            )

    async def generation_coverage(
        self,
        principal: Principal,
        space_ids: Sequence[str],
        generation_id: UUID,
    ) -> float:
        if not space_ids:
            return 0.0
        member_id = self._member_id(principal)
        statement = text(
            "SELECT COALESCE("
            "count(*) FILTER (WHERE ru.embedding IS NOT NULL)::double precision "
            "/ NULLIF(count(*), 0), 0.0) "
            + _AUTHORIZED_SOURCE
            + "WHERE ru.active "
            "AND ru.embedding_generation_id = :generation_id "
            "AND ru.space_id = ANY(CAST(:space_ids AS text[]))"
        )
        async with principal_session(self._session_factory, principal) as session:
            value = await session.scalar(
                statement,
                {
                    "member_id": member_id,
                    "space_ids": list(space_ids),
                    "generation_id": generation_id,
                },
            )
        return float(value or 0.0)

    async def lexical_search(
        self,
        principal: Principal,
        space_ids: Sequence[str],
        query: str,
        generation_id: UUID,
        limit: int,
        minimum_score: float,
    ) -> list[RetrievalCandidate]:
        if not space_ids or limit <= 0:
            return []
        member_id = self._member_id(principal)
        statement = text(
            "WITH query_value AS ("
            "SELECT websearch_to_tsquery('simple', :query) AS value"
            "), ranked AS ("
            "SELECT "
            + _CANDIDATE_COLUMNS
            + ", GREATEST("
            "ts_rank_cd(ru.tsv, q.value, 32), "
            "similarity(ru.title, :query) * 0.8, "
            "similarity(ru.content, :query) * 0.4, "
            "CASE WHEN ru.title ILIKE ('%' || :query || '%') THEN 0.5 ELSE 0.0 END, "
            "CASE WHEN ru.content ILIKE ('%' || :query || '%') THEN 0.35 ELSE 0.0 END"
            ") AS lexical_score "
            + _AUTHORIZED_SOURCE
            + "CROSS JOIN query_value q "
            "WHERE ru.active "
            "AND ru.embedding_generation_id = :generation_id "
            "AND ru.space_id = ANY(CAST(:space_ids AS text[])) "
            "AND (ru.tsv @@ q.value "
            "OR ru.title % :query OR ru.content % :query "
            "OR ru.title ILIKE ('%' || :query || '%') "
            "OR ru.content ILIKE ('%' || :query || '%'))"
            ") SELECT * FROM ranked "
            "WHERE lexical_score >= :minimum_score "
            "AND canonical_id IS NOT NULL AND revision_id IS NOT NULL "
            "ORDER BY lexical_score DESC, unit_id ASC LIMIT :limit"
        )
        async with principal_session(self._session_factory, principal) as session:
            rows = (
                await session.execute(
                    statement,
                    {
                        "member_id": member_id,
                        "space_ids": list(space_ids),
                        "query": query,
                        "generation_id": generation_id,
                        "minimum_score": minimum_score,
                        "limit": limit,
                    },
                )
            ).mappings()
            return [self._candidate(row, lexical_score=row["lexical_score"]) for row in rows]

    async def vector_search(
        self,
        principal: Principal,
        space_ids: Sequence[str],
        query_vector: Sequence[float],
        generation_id: UUID,
        limit: int,
        minimum_similarity: float,
        exact: bool,
        hnsw_ef_search: int,
    ) -> list[RetrievalCandidate]:
        if not space_ids or limit <= 0:
            return []
        member_id = self._member_id(principal)
        parameters = self._vector_parameters(
            member_id,
            space_ids,
            query_vector,
            generation_id,
            limit,
            minimum_similarity,
        )
        async with principal_session(self._session_factory, principal) as session:
            if exact:
                await session.execute(text("SET LOCAL enable_indexscan = off"))
                await session.execute(text("SET LOCAL enable_bitmapscan = off"))
            else:
                resolved_ef_search = min(hnsw_ef_search, 1000)
                await session.execute(text("SET LOCAL enable_sort = off"))
                await session.execute(text(f"SET LOCAL hnsw.ef_search = {resolved_ef_search}"))
                await session.execute(
                    text(f"SET LOCAL hnsw.max_scan_tuples = {max(20000, resolved_ef_search * 1000)}")
                )
                await session.execute(text("SET LOCAL hnsw.scan_mem_multiplier = 4"))
                await session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
            rows = (
                await session.execute(
                    text(_VECTOR_SEARCH_SQL),
                    parameters,
                )
            ).mappings()
            return [self._candidate(row, vector_similarity=row["vector_similarity"]) for row in rows]

    async def vector_search_plan(
        self,
        principal: Principal,
        space_ids: Sequence[str],
        query_vector: Sequence[float],
        generation_id: UUID,
        limit: int,
        minimum_similarity: float,
        hnsw_ef_search: int,
    ) -> object:
        if not space_ids or limit <= 0:
            return []
        member_id = self._member_id(principal)
        parameters = self._vector_parameters(
            member_id,
            space_ids,
            query_vector,
            generation_id,
            limit,
            minimum_similarity,
        )
        async with principal_session(self._session_factory, principal) as session:
            resolved_ef_search = min(hnsw_ef_search, 1000)
            await session.execute(text("SET LOCAL enable_sort = off"))
            await session.execute(text(f"SET LOCAL hnsw.ef_search = {resolved_ef_search}"))
            await session.execute(
                text(f"SET LOCAL hnsw.max_scan_tuples = {max(20000, resolved_ef_search * 1000)}")
            )
            await session.execute(text("SET LOCAL hnsw.scan_mem_multiplier = 4"))
            await session.execute(text("SET LOCAL hnsw.iterative_scan = 'strict_order'"))
            return await session.scalar(
                text("EXPLAIN (FORMAT JSON, COSTS OFF) " + _VECTOR_SEARCH_SQL),
                parameters,
            )

    @staticmethod
    def _vector_parameters(
        member_id: UUID,
        space_ids: Sequence[str],
        query_vector: Sequence[float],
        generation_id: UUID,
        limit: int,
        minimum_similarity: float,
    ) -> dict[str, object]:
        vector = [float(value) for value in query_vector]
        if len(vector) != EMBED_DIM or any(not math.isfinite(value) for value in vector):
            raise ValueError(f"Query embedding must contain {EMBED_DIM} finite values")
        return {
            "member_id": member_id,
            "space_ids": list(space_ids),
            "query_vector": "[" + ",".join(format(value, ".17g") for value in vector) + "]",
            "generation_id": generation_id,
            "maximum_distance": 1.0 - minimum_similarity,
            "candidate_limit": min(max(limit * 4, limit), 1000),
            "limit": limit,
        }

    @staticmethod
    def _member_id(principal: Principal) -> UUID:
        if (
            not principal.active
            or principal.restricted
            or ("*" not in principal.scopes and "knowledge:read" not in principal.scopes)
        ):
            raise AuthorizationException()
        try:
            return UUID(principal.subject_id)
        except ValueError as exc:
            raise AuthorizationException() from exc

    @staticmethod
    def _candidate(
        row,
        *,
        lexical_score: float | None = None,
        vector_similarity: float | None = None,
    ) -> RetrievalCandidate:
        return RetrievalCandidate(
            unit_id=row["unit_id"],
            source_type=row["source_type"],
            space_id=row["space_id"],
            canonical_id=row["canonical_id"],
            revision_id=row["revision_id"],
            title=row["title"],
            content=row["content"],
            embedding_generation_id=row["embedding_generation_id"],
            language=row["language"],
            chunk_id=row["chunk_id"],
            chunk_index=row["chunk_index"],
            source_filename=row["source_filename"],
            parser_version=row["parser_version"],
            version=row["version"],
            lexical_score=float(lexical_score) if lexical_score is not None else None,
            vector_similarity=float(vector_similarity) if vector_similarity is not None else None,
            source_metadata=dict(row["source_metadata"] or {}),
        )
