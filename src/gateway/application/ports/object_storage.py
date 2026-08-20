from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class StagedObject:
    storage_key: str
    size_bytes: int
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class StoredObject:
    storage_key: str
    size_bytes: int
    modified_at: datetime


class IVersionedObjectStorage(ABC):
    @abstractmethod
    async def stage(
        self,
        *,
        space_id: str,
        upload_id: UUID,
        chunks: AsyncIterable[bytes],
        max_bytes: int,
    ) -> StagedObject:
        ...

    @abstractmethod
    async def finalize(
        self,
        staging_key: str,
        *,
        space_id: str,
        document_id: UUID,
        revision_id: UUID,
    ) -> str:
        ...

    @abstractmethod
    async def read(self, storage_key: str) -> bytes:
        ...

    @abstractmethod
    async def exists(self, storage_key: str) -> bool:
        ...

    @abstractmethod
    async def delete(self, storage_key: str) -> bool:
        ...

    @abstractmethod
    async def list_objects(self, prefix: str) -> tuple[StoredObject, ...]:
        ...
