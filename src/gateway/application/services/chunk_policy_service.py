from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.config import get_settings
from src.gateway.domain.exceptions import ValidationException
from src.gateway.infrastructure.persistence.models import Workspace


CHUNK_STRATEGIES = ("fixed", "semantic")
MIN_CHUNK_SIZE = 64
MAX_CHUNK_SIZE = 32000


@dataclass(frozen=True, slots=True)
class ChunkPolicy:
    chunk_size: int
    chunk_overlap: int
    chunk_strategy: str
    source: str


def validate_chunk_policy(*, chunk_size: int, chunk_overlap: int, chunk_strategy: str) -> None:
    if chunk_strategy not in CHUNK_STRATEGIES:
        raise ValidationException("Unknown chunk strategy.")
    if chunk_size < MIN_CHUNK_SIZE or chunk_size > MAX_CHUNK_SIZE:
        raise ValidationException("Chunk size is outside the allowed range.")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValidationException("Chunk overlap must be less than chunk size.")


def global_chunk_policy() -> ChunkPolicy:
    settings = get_settings().gateway
    return ChunkPolicy(
        chunk_size=settings.ingestion_chunk_size,
        chunk_overlap=settings.ingestion_chunk_overlap,
        chunk_strategy="semantic",
        source="global",
    )


async def effective_chunk_policy(session: AsyncSession, space_id: str) -> ChunkPolicy:
    space = await session.get(Workspace, space_id)
    if (
        space is not None
        and space.chunk_size is not None
        and space.chunk_overlap is not None
        and space.chunk_strategy is not None
    ):
        return ChunkPolicy(
            chunk_size=space.chunk_size,
            chunk_overlap=space.chunk_overlap,
            chunk_strategy=space.chunk_strategy,
            source="space",
        )
    return global_chunk_policy()


def chunk_policy_payload(policy: ChunkPolicy) -> dict:
    return {
        "chunk_size": policy.chunk_size,
        "chunk_overlap": policy.chunk_overlap,
        "chunk_strategy": policy.chunk_strategy,
        "source": policy.source,
    }
