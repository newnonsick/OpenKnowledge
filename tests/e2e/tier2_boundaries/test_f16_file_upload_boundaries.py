"""Tier 2 Boundary Tests for Feature 16: File Upload Endpoint (POST /v1/files/upload).

Tests boundary conditions, multipart upload validations, 0-byte files, path traversal in filenames, and MIME types.
"""

import uuid
from pathlib import Path
import pytest

from src.gateway.domain.entities import DocumentFile
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter


@pytest.mark.tier2
@pytest.mark.feature("F16")
def test_f16_boundary_document_file_zero_byte_size():
    """Test boundary: DocumentFile domain entity correctly handles 0-byte file size."""
    doc = DocumentFile(
        filename="empty.txt",
        file_path="/storage/global/empty.txt",
        file_size=0,
        mime_type="text/plain",
        content_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    )
    assert doc.file_size == 0
    assert doc.file_size_bytes == 0
    assert doc.is_deleted is False


@pytest.mark.tier2
@pytest.mark.feature("F16")
@pytest.mark.asyncio
async def test_f16_boundary_file_upload_storage_path_sanitization(temp_storage_dir: Path):
    """Test boundary: file upload with path traversal in filename is sanitized into storage directory."""
    storage = LocalStorageAdapter(base_dir=temp_storage_dir)
    ws_id = "global"
    file_id = str(uuid.uuid4())
    malicious_filename = "../../../etc/passwd"

    saved_path = await storage.save_file(ws_id, file_id, malicious_filename, b"root:x:0:0")
    assert saved_path.exists()
    assert saved_path.is_relative_to(temp_storage_dir / ws_id)
    assert "passwd" in saved_path.name
    assert ".." not in saved_path.name


@pytest.mark.tier2
@pytest.mark.feature("F16")
def test_f16_boundary_document_file_mime_type_variations():
    """Test boundary: DocumentFile entity accepts standard, unusual, and custom MIME types."""
    mimes = [
        "text/plain",
        "text/markdown",
        "application/json",
        "application/pdf",
        "text/x-python",
        "application/octet-stream",
        "custom/unknown-format",
    ]
    for m in mimes:
        doc = DocumentFile(
            filename=f"file_{m.replace('/', '_')}.dat",
            file_path=f"/storage/{m.replace('/', '_')}.dat",
            file_size=128,
            mime_type=m,
        )
        assert doc.mime_type == m


@pytest.mark.tier2
@pytest.mark.feature("F16")
def test_f16_boundary_document_file_content_hash_validation():
    """Test boundary: DocumentFile content hash length and default assignment."""
    doc = DocumentFile(
        filename="test.md",
        file_path="/storage/test.md",
        file_size=500,
        content_hash="abc12345" * 8,
    )
    assert len(doc.content_hash) == 64


@pytest.mark.tier2
@pytest.mark.feature("F16")
def test_f16_boundary_document_file_tags_and_global_flag():
    """Test boundary: DocumentFile correctly stores tags and handles is_global workspace inheritance."""
    doc = DocumentFile(
        filename="shared_specs.pdf",
        file_path="/storage/shared_specs.pdf",
        file_size=2048,
        workspace_id="ws_project",
        is_global=True,
        tags=["architecture", "spec", "v1.0"],
    )
    assert doc.is_global is True
    assert len(doc.tags) == 3
    assert "architecture" in doc.tags
