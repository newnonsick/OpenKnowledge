from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.gateway.application.ports.object_storage import IVersionedObjectStorage
from src.gateway.infrastructure.persistence.ingestion_models import DocumentRevisionModel


@dataclass(frozen=True, slots=True)
class StorageManifestEntry:
    space_id: str
    document_id: UUID
    revision_id: UUID
    storage_key: str
    size_bytes: int
    checksum_sha256: str


@dataclass(frozen=True, slots=True)
class StorageManifest:
    version: int
    created_at: datetime
    entries: tuple[StorageManifestEntry, ...]
    digest_sha256: str

    def to_json(self) -> str:
        payload = {
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "entries": [
                {
                    **asdict(entry),
                    "document_id": str(entry.document_id),
                    "revision_id": str(entry.revision_id),
                }
                for entry in self.entries
            ],
            "digest_sha256": self.digest_sha256,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> StorageManifest:
        payload = json.loads(value)
        entries = tuple(
            StorageManifestEntry(
                space_id=str(item["space_id"]),
                document_id=UUID(item["document_id"]),
                revision_id=UUID(item["revision_id"]),
                storage_key=str(item["storage_key"]),
                size_bytes=int(item["size_bytes"]),
                checksum_sha256=str(item["checksum_sha256"]),
            )
            for item in payload["entries"]
        )
        return cls(
            version=int(payload["version"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
            entries=entries,
            digest_sha256=str(payload["digest_sha256"]),
        )


@dataclass(frozen=True, slots=True)
class StorageManifestVerification:
    valid: bool
    checked_objects: int
    missing_keys: tuple[str, ...]
    invalid_size_keys: tuple[str, ...]
    invalid_checksum_keys: tuple[str, ...]
    manifest_digest_valid: bool


class StorageManifestService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        storage: IVersionedObjectStorage,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage

    async def create(self) -> StorageManifest:
        async with self._session_factory.begin() as session:
            created_at = await session.scalar(select(func.now()))
            rows = (
                await session.execute(
                    select(
                        DocumentRevisionModel.space_id,
                        DocumentRevisionModel.document_id,
                        DocumentRevisionModel.id,
                        DocumentRevisionModel.storage_key,
                        DocumentRevisionModel.size_bytes,
                        DocumentRevisionModel.checksum_sha256,
                    )
                    .where(DocumentRevisionModel.storage_key.is_not(None))
                    .order_by(DocumentRevisionModel.space_id, DocumentRevisionModel.document_id, DocumentRevisionModel.version)
                )
            ).all()
        entries = tuple(
            StorageManifestEntry(
                space_id=row.space_id,
                document_id=row.document_id,
                revision_id=row.id,
                storage_key=row.storage_key,
                size_bytes=row.size_bytes,
                checksum_sha256=row.checksum_sha256,
            )
            for row in rows
        )
        normalized_created_at = created_at or datetime.now(timezone.utc)
        digest = self._digest(1, normalized_created_at, entries)
        return StorageManifest(1, normalized_created_at, entries, digest)

    async def verify(self, manifest: StorageManifest) -> StorageManifestVerification:
        digest_valid = manifest.digest_sha256 == self._digest(
            manifest.version,
            manifest.created_at,
            manifest.entries,
        )
        missing = []
        invalid_size = []
        invalid_checksum = []
        for entry in manifest.entries:
            if not await self._storage.exists(entry.storage_key):
                missing.append(entry.storage_key)
                continue
            content = await self._storage.read(entry.storage_key)
            if len(content) != entry.size_bytes:
                invalid_size.append(entry.storage_key)
                continue
            if hashlib.sha256(content).hexdigest() != entry.checksum_sha256:
                invalid_checksum.append(entry.storage_key)
        return StorageManifestVerification(
            valid=digest_valid and not missing and not invalid_size and not invalid_checksum,
            checked_objects=len(manifest.entries),
            missing_keys=tuple(missing),
            invalid_size_keys=tuple(invalid_size),
            invalid_checksum_keys=tuple(invalid_checksum),
            manifest_digest_valid=digest_valid,
        )

    @staticmethod
    def _digest(
        version: int,
        created_at: datetime,
        entries: tuple[StorageManifestEntry, ...],
    ) -> str:
        payload = {
            "version": version,
            "created_at": created_at.isoformat(),
            "entries": [
                {
                    "space_id": entry.space_id,
                    "document_id": str(entry.document_id),
                    "revision_id": str(entry.revision_id),
                    "storage_key": entry.storage_key,
                    "size_bytes": entry.size_bytes,
                    "checksum_sha256": entry.checksum_sha256,
                }
                for entry in entries
            ],
        }
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
