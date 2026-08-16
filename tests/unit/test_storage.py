"""Unit tests for LocalStorageAdapter."""

import io
from pathlib import Path
import pytest

from src.gateway.domain.exceptions import ItemNotFoundException, StorageException
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter


@pytest.mark.asyncio
async def test_save_and_read_file(local_storage: LocalStorageAdapter, temp_storage_dir: Path):
    """Test saving bytes and reading back."""
    workspace_id = "ws_test"
    file_id = "file_001"
    filename = "report.txt"
    content = b"Sample binary file payload 12345"

    saved_path = await local_storage.save_file(workspace_id, file_id, filename, content)
    assert saved_path.exists()
    assert saved_path == temp_storage_dir / workspace_id / f"{file_id}_{filename}"

    read_bytes = await local_storage.read_file(workspace_id, file_id, filename)
    assert read_bytes == content


@pytest.mark.asyncio
async def test_save_stream_file(local_storage: LocalStorageAdapter):
    """Test saving from BinaryIO stream."""
    stream = io.BytesIO(b"Stream data chunk")
    saved_path = await local_storage.save_file("ws_stream", "stream_01", "data.bin", stream)
    assert saved_path.exists()

    read_bytes = await local_storage.read_file("ws_stream", "stream_01", "data.bin")
    assert read_bytes == b"Stream data chunk"


@pytest.mark.asyncio
async def test_file_exists_and_delete(local_storage: LocalStorageAdapter):
    """Test existence checking and deletion."""
    assert not await local_storage.exists("ws_del", "f1", "doc.md")
    assert not await local_storage.file_exists("ws_del", "f1", "doc.md")

    await local_storage.save_file("ws_del", "f1", "doc.md", b"Content")
    assert await local_storage.exists("ws_del", "f1", "doc.md")
    assert await local_storage.file_exists("ws_del", "f1", "doc.md")

    deleted = await local_storage.delete_file("ws_del", "f1", "doc.md")
    assert deleted is True
    assert not await local_storage.exists("ws_del", "f1", "doc.md")

    # Delete non-existent returns False
    deleted_again = await local_storage.delete_file("ws_del", "f1", "doc.md")
    assert deleted_again is False


@pytest.mark.asyncio
async def test_workspace_isolation(local_storage: LocalStorageAdapter):
    """Test that files with identical IDs in different workspaces are isolated."""
    await local_storage.save_file("ws_a", "id_1", "file.txt", b"Workspace A Data")
    await local_storage.save_file("ws_b", "id_1", "file.txt", b"Workspace B Data")

    assert await local_storage.read_file("ws_a", "id_1", "file.txt") == b"Workspace A Data"
    assert await local_storage.read_file("ws_b", "id_1", "file.txt") == b"Workspace B Data"


@pytest.mark.asyncio
async def test_path_traversal_prevention_workspace_id(local_storage: LocalStorageAdapter):
    """Test path traversal attempts via workspace_id are blocked."""
    invalid_ids = ["../etc", "..", "ws/nested", "ws\\nested", "ws*name", "ws\x00", ""]
    for bad_id in invalid_ids:
        with pytest.raises(StorageException):
            await local_storage.save_file(bad_id, "f1", "test.txt", b"Bad data")


@pytest.mark.asyncio
async def test_invalid_file_id(local_storage: LocalStorageAdapter):
    """Test invalid file IDs are rejected."""
    with pytest.raises(StorageException):
        await local_storage.save_file("ws_valid", "../bad_file_id", "test.txt", b"data")


@pytest.mark.asyncio
async def test_path_traversal_prevention_filename(local_storage: LocalStorageAdapter, temp_storage_dir: Path):
    """Test path traversal attempts via filename are safely sanitized."""
    path = await local_storage.save_file("safe_ws", "f_sec", "../../passwd", b"root:x:0:0")
    assert path.is_relative_to(temp_storage_dir / "safe_ws")
    assert path.name == "f_sec_passwd"


@pytest.mark.asyncio
async def test_filename_sanitization_characters(local_storage: LocalStorageAdapter, temp_storage_dir: Path):
    """Test sanitization of null bytes and Windows/Linux reserved chars."""
    path = await local_storage.save_file("ws_sanitize", "f1", "my:bad*file?name.txt\x00", b"test")
    assert "\x00" not in path.name
    assert ":" not in path.name
    assert "*" not in path.name
    assert "?" not in path.name
    assert path.is_relative_to(temp_storage_dir / "ws_sanitize")

    # Empty filename fallback
    path_empty = await local_storage.save_file("ws_sanitize", "f2", "", b"empty filename data")
    assert "unnamed_file" in path_empty.name


@pytest.mark.asyncio
async def test_read_nonexistent_file_raises_not_found(local_storage: LocalStorageAdapter):
    """Test reading non-existent file raises ItemNotFoundException."""
    with pytest.raises(ItemNotFoundException):
        await local_storage.read_file("ws_test", "nonexistent", "nofile.txt")


@pytest.mark.asyncio
async def test_get_path(local_storage: LocalStorageAdapter, temp_storage_dir: Path):
    """Test get_path resolution."""
    path = await local_storage.get_path("ws_path", "f123", "data.json")
    assert path == temp_storage_dir / "ws_path" / "f123_data.json"


@pytest.mark.asyncio
async def test_delete_workspace(local_storage: LocalStorageAdapter, temp_storage_dir: Path):
    """Test recursive deletion of entire workspace directory."""
    await local_storage.save_file("ws_purge", "f1", "a.txt", b"AAA")
    await local_storage.save_file("ws_purge", "f2", "b.txt", b"BBB")

    ws_path = temp_storage_dir / "ws_purge"
    assert ws_path.exists()

    result = await local_storage.delete_workspace("ws_purge")
    assert result is True
    assert not ws_path.exists()

    # Deleting already-deleted workspace returns False
    assert await local_storage.delete_workspace("ws_purge") is False
