from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID


SourceType = Literal["knowledge_revision", "document_chunk"]
SemanticPolicy = Literal["prefer", "required", "disabled"]


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    unit_id: UUID
    source_type: SourceType
    space_id: str
    canonical_id: UUID
    revision_id: UUID
    title: str
    content: str
    embedding_generation_id: UUID
    language: str | None = None
    chunk_id: UUID | None = None
    chunk_index: int | None = None
    source_filename: str | None = None
    parser_version: str | None = None
    version: int | None = None
    lexical_score: float | None = None
    vector_similarity: float | None = None
    source_metadata: dict = field(default_factory=dict)

    @property
    def source_key(self) -> str:
        return f"{self.source_type}:{self.canonical_id}"

    @property
    def citation_uri(self) -> str:
        if self.source_type == "knowledge_revision":
            return (
                f"openknowledge://spaces/{self.space_id}/knowledge/{self.canonical_id}"
                f"/revisions/{self.revision_id}"
            )
        suffix = f"/chunks/{self.chunk_id}" if self.chunk_id else ""
        return (
            f"openknowledge://spaces/{self.space_id}/documents/{self.canonical_id}"
            f"/revisions/{self.revision_id}{suffix}"
        )


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    rank: int
    candidate: RetrievalCandidate
    rrf_score: float
    rank_score: float
    lexical_rank: int | None
    vector_rank: int | None
    active_space_boost: float


@dataclass(frozen=True, slots=True)
class RetrievalHealth:
    semantic_status: Literal["active", "disabled", "degraded"]
    degraded_reasons: tuple[str, ...]
    embedding_generation_id: UUID | None
    embedding_coverage: float | None


@dataclass(frozen=True, slots=True)
class RetrievalExplanation:
    effective_space_ids: tuple[str, ...]
    lexical_candidates: tuple[RetrievalCandidate, ...]
    vector_candidates: tuple[RetrievalCandidate, ...]
    source_diversity_limit: int
    active_space_id: str | None
    abstained: bool


@dataclass(frozen=True, slots=True)
class RetrievalResponse:
    query: str
    hits: tuple[RetrievalHit, ...]
    health: RetrievalHealth
    explanation: RetrievalExplanation
