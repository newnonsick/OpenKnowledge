"""Tier 2 Boundary Tests for Feature 6: Local Disk Storage Adapter.

Tests boundary conditions, path traversal prevention, 0-byte files, large files, and missing files.
"""

from pathlib import Path
import pytest

from src.gateway.domain.exceptions import ItemNotFoundException, StorageException
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter


@pytest.mark.tier2
@pytest.mark.feature("F6")
@pytest.mark.asyncio
async def test_f06_boundary_path_traversal_rejection(temp_storage_dir: Path):
    """Test boundary: path traversal sequences in workspace_id or file_id raise StorageException."""
    adapter = LocalStorageAdapter(base_dir=temp_storage_dir)

    traversal_workspaces = [
        "../",
        "../../etc/passwd",
        "..\\..\\windows\\system32",
        "ws/../../../root",
        "/etc/shadow",
        "C:\\secrets",
    ]

    for malicious_ws in traversal_workspaces:
        with pytest.raises(StorageException):
            await adapter.save_file(malicious_ws, "fid1", "test.txt", b"payload")

        with pytest.raises(StorageException):
            await adapter.read_file(malicious_ws, "fid1", "test.txt")

        with pytest.raises(StorageException):
            await adapter.delete_file(malicious_ws, "fid1", "test.txt")


@pytest.mark.tier2
@pytest.mark.feature("F6")
@pytest.mark.asyncio
async def test_f06_boundary_zero_byte_file_save_and_read(temp_storage_dir: Path):
    """Test boundary: saving and reading an empty 0-byte file succeeds."""
    adapter = LocalStorageAdapter(base_dir=temp_storage_dir)
    ws_id = "ws_zero_byte"
    file_id = "fid_empty"
    filename = "empty.txt"

    path = await adapter.save_file(ws_id, file_id, filename, b"")
    assert path.exists()
    assert path.stat().st_size == 0

    read_data = await adapter.read_file(ws_id, file_id, filename)
    assert read_data == b""
    assert await adapter.exists(ws_id, file_id, filename) is True


@pytest.mark.tier2
@pytest.mark.feature("F6")
@pytest.mark.asyncio
async def test_f06_boundary_large_file_save_and_read(temp_storage_dir: Path):
    """Test boundary: saving and reading a 5MB payload in chunks."""
    adapter = LocalStorageAdapter(base_dir=temp_storage_dir)
    ws_id = "ws_large_payload"
    file_id = "fid_large"
    filename = "large_binary.bin"

    large_payload = b"X" * (5 * 1024 * 1024)  # 5 MB
    path = await adapter.save_file(ws_id, file_id, filename, large_payload)
    assert path.exists()
    assert path.stat().st_size == len(large_payload)

    read_data = await adapter.read_file(ws_id, file_id, filename)
    assert len(read_data) == len(large_payload)
    assert read_data == large_payload


@pytest.mark.tier2
@pytest.mark.feature("F6")
@pytest.mark.asyncio
async def test_f06_boundary_non_existent_file_and_workspace(temp_storage_dir: Path):
    """Test boundary: reading or deleting non-existent files returns ItemNotFoundException or False."""
    adapter = LocalStorageAdapter(base_dir=temp_storage_dir)

    with pytest.raises(ItemNotFoundException):
        await adapter.read_file("non_existent_ws", "fid_999", "missing.txt")

    assert await adapter.exists("non_existent_ws", "fid_999", "missing.txt") is False
    assert await adapter.delete_file("non_existent_ws", "fid_999", "missing.txt") is False


@pytest.mark.tier2
@pytest.mark.feature("F6")
@pytest.mark.asyncio
async def test_f06_boundary_special_characters_in_filename_sanitization(temp_storage_dir: Path):
    """Test boundary: filenames with special characters (spaces, colons, emojis) are safely sanitized."""
    adapter = LocalStorageAdapter(base_dir=temp_storage_dir)
    ws_id = "ws_special_chars"
    file_id = "fid_special"
    dirty_filename = "test:file*with?quotes\"<and>pipes|.txt"

    saved_path = await adapter.save_file(ws_id, file_id, dirty_filename, b"special payload")
    assert saved_path.exists()
    # Check invalid Windows/POSIX filename characters are sanitized
    assert ":" not in saved_path.name
    assert "*" not in saved_path.name
    assert "?" not in saved_path.name
    assert "<" not in saved_path.name
    assert ">" not in saved_path.name
    assert "|" not in saved_path.name

    # Reading back using original requested filename still resolves correctly
    read_data = await adapter.read_file(ws_id, file_id, dirty_filename)
    assert read_data == b"special payload"
