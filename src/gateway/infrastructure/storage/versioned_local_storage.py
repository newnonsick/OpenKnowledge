from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
import re
from uuid import UUID

from src.gateway.application.ports.object_storage import IVersionedObjectStorage, StagedObject, StoredObject
from src.gateway.domain.exceptions import ItemNotFoundException, StorageException, UploadTooLargeException


_SEGMENT = re.compile(r"\A[a-zA-Z0-9_-]+\Z")


class LocalVersionedObjectStorage(IVersionedObjectStorage):
    def __init__(self, base_dir: str | Path) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._base_dir.mkdir(parents=True, exist_ok=True)

    async def stage(
        self,
        *,
        space_id: str,
        upload_id: UUID,
        chunks,
        max_bytes: int,
    ) -> StagedObject:
        if max_bytes <= 0:
            raise ValueError("A positive upload limit is required")
        key = f"staging/{self._segment(space_id)}/{upload_id}"
        path = self._path(key)
        await asyncio.to_thread(self._create_empty, path)
        digest = hashlib.sha256()
        size = 0
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise StorageException("Upload chunks must be bytes.")
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLargeException()
                digest.update(chunk)
                await asyncio.to_thread(self._append, path, chunk)
            await asyncio.to_thread(self._sync, path)
            return StagedObject(
                storage_key=key,
                size_bytes=size,
                checksum_sha256=digest.hexdigest(),
            )
        except Exception:
            await asyncio.to_thread(path.unlink, missing_ok=True)
            raise

    async def finalize(
        self,
        staging_key: str,
        *,
        space_id: str,
        document_id: UUID,
        revision_id: UUID,
    ) -> str:
        expected_prefix = f"staging/{self._segment(space_id)}/"
        if not staging_key.startswith(expected_prefix):
            raise StorageException("The staging object does not belong to the requested space.")
        source = self._path(staging_key)
        final_key = f"objects/{self._segment(space_id)}/{document_id}/{revision_id}"
        target = self._path(final_key)
        await asyncio.to_thread(self._link_and_unlink, source, target)
        return final_key

    async def read(self, storage_key: str) -> bytes:
        path = self._path(storage_key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise ItemNotFoundException() from exc
        except OSError as exc:
            raise StorageException("Failed to read the storage object.") from exc

    async def exists(self, storage_key: str) -> bool:
        path = self._path(storage_key)
        return await asyncio.to_thread(lambda: path.is_file())

    async def delete(self, storage_key: str) -> bool:
        path = self._path(storage_key)
        if not await asyncio.to_thread(path.is_file):
            return False
        try:
            await asyncio.to_thread(path.unlink)
            return True
        except OSError as exc:
            raise StorageException("Failed to delete the storage object.") from exc

    async def list_objects(self, prefix: str) -> tuple[StoredObject, ...]:
        if prefix not in {"staging", "objects"}:
            raise StorageException("Invalid storage inventory prefix.")
        return await asyncio.to_thread(self._list_objects, prefix)

    def _path(self, storage_key: str) -> Path:
        parts = storage_key.split("/")
        if len(parts) < 3 or parts[0] not in {"staging", "objects"}:
            raise StorageException("Invalid storage key.")
        if any(not self._segment(part) for part in parts[1:]):
            raise StorageException("Invalid storage key.")
        path = (self._base_dir.joinpath(*parts)).resolve()
        try:
            path.relative_to(self._base_dir)
        except ValueError as exc:
            raise StorageException("Storage key escapes the configured root.") from exc
        return path

    def _segment(self, value) -> str:
        segment = str(value)
        if not _SEGMENT.fullmatch(segment):
            raise StorageException("Invalid storage key segment.")
        return segment

    def _create_empty(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb"):
                pass
        except FileExistsError as exc:
            raise StorageException("The staging object already exists.") from exc
        except OSError as exc:
            raise StorageException("Failed to create the staging object.") from exc

    def _append(self, path: Path, chunk: bytes) -> None:
        try:
            with path.open("ab") as handle:
                handle.write(chunk)
        except OSError as exc:
            raise StorageException("Failed to write the staging object.") from exc

    def _sync(self, path: Path) -> None:
        try:
            with path.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise StorageException("Failed to finalize the staging object.") from exc

    def _link_and_unlink(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            if source.is_file():
                try:
                    if not os.path.samefile(source, target):
                        raise StorageException("The immutable storage object already exists.")
                    source.unlink()
                except OSError as exc:
                    raise StorageException("Failed to finalize the storage object.") from exc
            return
        if not source.is_file():
            raise ItemNotFoundException()
        try:
            os.link(source, target)
            source.unlink()
        except FileExistsError as exc:
            raise StorageException("The immutable storage object already exists.") from exc
        except OSError as exc:
            if target.exists() and source.exists():
                target.unlink(missing_ok=True)
            raise StorageException("Failed to finalize the storage object.") from exc

    def _list_objects(self, prefix: str) -> tuple[StoredObject, ...]:
        root = self._base_dir / prefix
        if not root.exists():
            return ()
        found = []
        for path in root.rglob("*"):
            if path.is_symlink():
                raise StorageException("Storage inventory contains a symbolic link.")
            if not path.is_file():
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self._base_dir)
                stat = resolved.stat()
                key = resolved.relative_to(self._base_dir).as_posix()
                self._path(key)
            except (OSError, ValueError) as exc:
                raise StorageException("Failed to inventory storage objects.") from exc
            found.append(
                StoredObject(
                    storage_key=key,
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                )
            )
        return tuple(sorted(found, key=lambda item: item.storage_key))
