from __future__ import annotations

from typing import Protocol, Sequence
from uuid import UUID

from src.gateway.domain.identity import Principal
from src.gateway.domain.retrieval import RetrievalCandidate


class RetrievalUnitRepository(Protocol):
    async def active_generation(self, principal: Principal) -> UUID | None:
        ...

    async def generation_coverage(
        self,
        principal: Principal,
        space_ids: Sequence[str],
        generation_id: UUID,
    ) -> float:
        ...

    async def lexical_search(
        self,
        principal: Principal,
        space_ids: Sequence[str],
        query: str,
        generation_id: UUID,
        limit: int,
        minimum_score: float,
    ) -> list[RetrievalCandidate]:
        ...

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
        ...
