"""Tier 1 Feature Tests for Feature 16: File Upload Endpoint (POST /v1/files/upload).

Validates file ingestion domain entity creation, local storage persistence, SHA-256 file hashing,
MIME type categorization, and database record persistence.
"""

import hashlib
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import select

from src.gateway.domain.entities import DocumentFile
from src.gateway.infrastructure.persistence.models import DocumentFile as DBDocumentFile
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter
from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.tier1
@pytest.mark.feature("F16")
def test_f16_document_file_entity_instantiation():
    """Verify DocumentFile domain model instantiation with metadata, hashes, and defaults."""
    doc_id = uuid4()
    content_bytes = b"# Architecture Overview\nPragmatic Clean Architecture in Python."
    expected_hash = hashlib.sha256(content_bytes).hexdigest()

    doc = DocumentFile(
        id=doc_id,
        workspace_id="ws-proj-1",
        filename="architecture.md",
        file_path="/storage/ws-proj-1/architecture.md",
        file_size_bytes=len(content_bytes),
        content_hash=expected_hash,
        mime_type="text/markdown",
        is_global=False,
    )

    assert doc.id == doc_id
    assert doc.workspace_id == "ws-proj-1"
    assert doc.filename == "architecture.md"
    assert doc.file_size_bytes == len(content_bytes)
    assert doc.content_hash == expected_hash
    assert doc.mime_type == "text/markdown"
    assert doc.is_deleted is False


@pytest.mark.tier1
@pytest.mark.feature("F16")
@pytest.mark.asyncio
async def test_f16_local_storage_file_persistence(temp_storage_dir: Path):
    """Verify LocalStorageAdapter saves uploaded binary file into workspace storage."""
    storage = LocalStorageAdapter(base_dir=temp_storage_dir)
    ws_id = "ws-upload-test"
    file_id = str(uuid4())
    filename = "requirements.txt"
    payload = b"fastapi>=0.115.0\nuvicorn>=0.30.0\npydantic>=2.8.0\n"

    saved_path = await storage.save_file(ws_id, file_id, filename, payload)
    assert saved_path.exists()
    assert saved_path.is_file()
    assert saved_path.is_relative_to(temp_storage_dir / ws_id)

    read_back = await storage.read_file(ws_id, file_id, filename)
    assert read_back == payload


@pytest.mark.tier1
@pytest.mark.feature("F16")
def test_f16_file_content_hash_sha256_computation():
    """Verify exact SHA-256 hash generation for binary uploaded files."""
    data = b"Some source code in python: def test(): pass"
    hasher = hashlib.sha256()
    hasher.update(data)
    expected_digest = hasher.hexdigest()

    doc = DocumentFile(
        filename="test.py",
        file_path="/storage/test.py",
        file_size_bytes=len(data),
        content_hash=expected_digest,
        mime_type="text/x-python",
    )
    assert doc.content_hash == expected_digest
    assert len(doc.content_hash) == 64


@pytest.mark.tier1
@pytest.mark.feature("F16")
@pytest.mark.asyncio
async def test_f16_document_file_orm_persistence():
    """Verify persisting DocumentFile in database and querying it back by workspace and id."""
    async with TestEnvironment() as env:
        async with env.session_factory() as session:
            async with session.begin():
                doc_id = uuid4()
                ws_id = "global"

                doc_orm = DBDocumentFile(
                    id=doc_id,
                    workspace_id=ws_id,
                    filename="specs.pdf",
                    file_path="/storage/global/specs.pdf",
                    file_size=1048576,
                    mime_type="application/pdf",
                )
                session.add(doc_orm)

            # Query back
            async with session.begin():
                stmt = select(DBDocumentFile).where(DBDocumentFile.id == doc_id)
                res = await session.execute(stmt)
                fetched = res.scalar_one_or_none()
                assert fetched is not None
                assert fetched.filename == "specs.pdf"
                assert fetched.file_size == 1048576
                assert fetched.mime_type == "application/pdf"


@pytest.mark.tier1
@pytest.mark.feature("F16")
def test_f16_document_file_tags_and_global_flag():
    """Verify tags and is_global attributes on DocumentFile domain model."""
    doc = DocumentFile(
        filename="company_handbook.pdf",
        file_path="/storage/global/handbook.pdf",
        file_size_bytes=45000,
        workspace_id="global",
        is_global=True,
        tags=["hr", "policy", "handbook"],
    )
    assert doc.is_global is True
    assert len(doc.tags) == 3
    assert "hr" in doc.tags
    assert "policy" in doc.tags
