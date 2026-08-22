import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from src.gateway.domain.exceptions import StorageException, UploadTooLargeException
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage


async def _chunks(*values: bytes):
    for value in values:
        yield value


async def test_stage_stream_enforces_limit_and_computes_checksum(tmp_path) -> None:
    storage = LocalVersionedObjectStorage(tmp_path)
    upload_id = uuid4()
    staged = await storage.stage(
        space_id="family",
        upload_id=upload_id,
        chunks=_chunks(b"hello", b" world"),
        max_bytes=11,
    )

    assert staged.size_bytes == 11
    assert staged.checksum_sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert staged.storage_key == f"staging/family/{upload_id}"
    assert await storage.read(staged.storage_key) == b"hello world"

    with pytest.raises(UploadTooLargeException):
        await storage.stage(
            space_id="family",
            upload_id=uuid4(),
            chunks=_chunks(b"1234", b"56"),
            max_bytes=5,
        )
    assert not any(path.is_file() and path.stat().st_size == 6 for path in tmp_path.rglob("*"))


async def test_stage_accepts_windows_normalized_containment_paths(tmp_path) -> None:
    root = tmp_path / "storage-root"
    storage = LocalVersionedObjectStorage(root)
    upload_id = uuid4()
    space_id = "windows" + "x" * 220

    staged = await storage.stage(
        space_id=space_id,
        upload_id=upload_id,
        chunks=_chunks(b"payload"),
        max_bytes=100,
    )

    assert staged.storage_key == f"staging/{space_id}/{upload_id}"
    assert await storage.exists(staged.storage_key) is True


async def test_finalize_is_immutable_and_generated_keys_cannot_escape_storage(tmp_path) -> None:
    storage = LocalVersionedObjectStorage(tmp_path)
    staged = await storage.stage(
        space_id="family",
        upload_id=uuid4(),
        chunks=_chunks(b"payload"),
        max_bytes=100,
    )
    document_id = uuid4()
    revision_id = uuid4()
    final_key = await storage.finalize(
        staged.storage_key,
        space_id="family",
        document_id=document_id,
        revision_id=revision_id,
    )

    assert final_key == f"objects/family/{document_id}/{revision_id}"
    assert await storage.read(final_key) == b"payload"
    assert not await storage.exists(staged.storage_key)

    second = await storage.stage(
        space_id="family",
        upload_id=uuid4(),
        chunks=_chunks(b"replacement"),
        max_bytes=100,
    )
    with pytest.raises(StorageException):
        await storage.finalize(
            second.storage_key,
            space_id="family",
            document_id=document_id,
            revision_id=revision_id,
        )
    assert await storage.read(final_key) == b"payload"

    with pytest.raises(StorageException):
        await storage.read("../../outside")


async def test_storage_inventory_reports_only_safe_regular_objects(tmp_path) -> None:
    storage = LocalVersionedObjectStorage(tmp_path)
    staged = await storage.stage(
        space_id="space-a",
        upload_id=uuid4(),
        chunks=_chunks(b"inventory"),
        max_bytes=100,
    )

    objects = await storage.list_objects("staging")

    assert [item.storage_key for item in objects] == [staged.storage_key]
    assert objects[0].size_bytes == len(b"inventory")
    assert objects[0].modified_at <= datetime.now(timezone.utc)
