"""Tier 1 Feature Tests for Feature 6: Local Disk Storage Adapter.

Validates atomic file persistence, byte and stream reading, existence verification,
path traversal prevention, and workspace directory cleanup.
"""

import io
from pathlib import Path
import pytest

from src.gateway.domain.exceptions import ItemNotFoundException, StorageException
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter


@pytest.mark.tier1
@pytest.mark.feature("F6")
@pytest.mark.asyncio
async def test_f06_save_and_read_bytes_roundtrip(temp_storage_dir: Path):
    """Verify saving raw bytes and reading them back accurately."""
    storage = LocalStorageAdapter(base_dir=temp_storage_dir)
    workspace_id = "ws_test_01"
    file_id = "f_alpha_1"
    filename = "document.txt"
    payload = b"Hello, this is a test document for LocalStorageAdapter."

    saved_path = await storage.save_file(workspace_id, file_id, filename, payload)
    assert saved_path.exists()
    assert saved_path.is_file()

    read_bytes = await storage.read_file(workspace_id, file_id, filename)
    assert read_bytes == payload


@pytest.mark.tier1
@pytest.mark.feature("F6")
@pytest.mark.asyncio
async def test_f06_save_stream_binary_io(temp_storage_dir: Path):
    """Verify saving from a BinaryIO stream (e.g. io.BytesIO)."""
    storage = LocalStorageAdapter(base_dir=temp_storage_dir)
    workspace_id = "ws_test_02"
    file_id = "f_stream_1"
    filename = "stream_data.bin"
    stream_content = b"\x00\x01\x02\x03\xff\xfe\xfd\xfc" * 64
    stream = io.BytesIO(stream_content)

    saved_path = await storage.save_file(workspace_id, file_id, filename, stream)
    assert saved_path.exists()

    read_bytes = await storage.read_file(workspace_id, file_id, filename)
    assert read_bytes == stream_content


@pytest.mark.tier1
@pytest.mark.feature("F6")
@pytest.mark.asyncio
async def test_f06_exists_and_delete_file(temp_storage_dir: Path):
    """Verify existence check and file deletion."""
    storage = LocalStorageAdapter(base_dir=temp_storage_dir)
    workspace_id = "ws_test_03"
    file_id = "f_del_1"
    filename = "temp.txt"

    # Pre-check: file does not exist
    assert await storage.exists(workspace_id, file_id, filename) is False

    # Save and check exists
    await storage.save_file(workspace_id, file_id, filename, b"temporary data")
    assert await storage.exists(workspace_id, file_id, filename) is True

    # Delete and check exists
    deleted = await storage.delete_file(workspace_id, file_id, filename)
    assert deleted is True
    assert await storage.exists(workspace_id, file_id, filename) is False

    # Deleting non-existent file returns False
    assert await storage.delete_file(workspace_id, file_id, filename) is False


@pytest.mark.tier1
@pytest.mark.feature("F6")
@pytest.mark.asyncio
async def test_f06_path_traversal_protection(temp_storage_dir: Path):
    """Verify that path traversal attempts in workspace_id, file_id, or filename are blocked."""
    storage = LocalStorageAdapter(base_dir=temp_storage_dir)

    # 1. Traversal in workspace_id
    with pytest.raises(StorageException):
        await storage.save_file("../outside_ws", "f1", "test.txt", b"evil")

    # 2. Traversal in file_id
    with pytest.raises(StorageException):
        await storage.save_file("ws1", "../../escape_file", "test.txt", b"evil")

    # 3. Filename sanitization prevents directory escape
    target_path = await storage.save_file("ws1", "f1", "../../../etc/passwd", b"safe")
    # Verified that target path stays strictly inside workspace directory
    assert temp_storage_dir in target_path.parents


@pytest.mark.tier1
@pytest.mark.feature("F6")
@pytest.mark.asyncio
async def test_f06_delete_workspace_cleanup(temp_storage_dir: Path):
    """Verify recursive cleanup of an entire workspace directory."""
    storage = LocalStorageAdapter(base_dir=temp_storage_dir)
    workspace_id = "ws_cleanup"

    # Save multiple files in the workspace
    await storage.save_file(workspace_id, "f1", "a.txt", b"file a")
    await storage.save_file(workspace_id, "f2", "b.txt", b"file b")

    ws_dir = await storage.get_path(workspace_id, "f1", "a.txt")
    assert ws_dir.parent.exists()

    # Delete entire workspace
    result = await storage.delete_workspace(workspace_id)
    assert result is True
    assert not ws_dir.parent.exists()

    # Deleting already deleted workspace returns False
    assert await storage.delete_workspace(workspace_id) is False
