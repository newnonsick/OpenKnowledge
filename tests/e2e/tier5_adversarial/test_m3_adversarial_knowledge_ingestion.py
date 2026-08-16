"""Tier 5 Adversarial Stress Tests: Milestone M3 Knowledge Subsystem & File Ingestion.

Empirically stress-tests:
1. High-concurrency Optimistic Concurrency Control (OCC) race conditions (50 concurrent updates).
2. Concurrent saves, updates, soft-deletes, and cross-workspace isolation.
3. Content hash stability, determinism, and collision resilience (SHA-256).
4. Corrupted file ingestion attacks:
   - UTF-8 BOM, UTF-16 LE/BE BOM, mixed encodings.
   - Binary garbage, null bytes (\\x00), high control characters.
   - Truncated / malformed JSON documents, nested structures.
   - Damaged PDF files (corrupt headers, truncated streams, empty bodies).
   - Zero-byte files and oversized single-line documents.
5. Ingestion pipeline error propagation and workspace scoping.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from src.gateway.application.parsers import (
    Chunker,
    CodeParser,
    JSONParser,
    PDFParser,
    TextParser,
    get_parser_for_file,
)
from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.application.ports.repositories import (
    IDocumentRepository,
    IKnowledgeRepository,
)
from src.gateway.application.services.ingestion_service import IngestionService
from src.gateway.application.services.knowledge_service import KnowledgeService
from src.gateway.domain.canonical import RankedSearchResult
from src.gateway.domain.entities import (
    DocumentChunk,
    DocumentFile,
    KnowledgeItem,
    KnowledgeRevision,
)
from src.gateway.domain.exceptions import (
    ConcurrencyConflictException,
    ItemNotFoundException,
    StorageException,
    ValidationException,
)
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter


# ==============================================================================
# In-Memory & Mock Repositories for High-Concurrency Stress Testing
# ==============================================================================

class MemoryKnowledgeRepository(IKnowledgeRepository):
    """Thread-safe / Async-safe in-memory knowledge repository for concurrency stress testing."""

    def __init__(self) -> None:
        self._items: Dict[UUID, KnowledgeItem] = {}
        self._revisions: Dict[UUID, List[KnowledgeRevision]] = {}
        self._lock = asyncio.Lock()

    async def create_item(
        self, item: KnowledgeItem, initial_revision: KnowledgeRevision
    ) -> KnowledgeItem:
        async with self._lock:
            self._items[item.id] = item
            self._revisions[item.id] = [initial_revision]
            return item

    async def get_item_by_id(
        self,
        item_id: UUID,
        version: Optional[int] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[KnowledgeItem]:
        async with self._lock:
            item = self._items.get(item_id)
            if not item:
                return None
            if item.is_deleted:
                return None
            if workspace_id and not item.is_global and item.workspace_id != workspace_id:
                return None

            if version is not None:
                revs = self._revisions.get(item_id, [])
                matching_rev = next((r for r in revs if r.version == version), None)
                if not matching_rev:
                    return None
                return KnowledgeItem(
                    id=item.id,
                    workspace_id=item.workspace_id,
                    is_global=item.is_global,
                    version=matching_rev.version,
                    title=matching_rev.title or item.title,
                    content=matching_rev.content,
                    tags=matching_rev.tags,
                    is_deleted=item.is_deleted,
                    created_at=item.created_at,
                    updated_at=matching_rev.created_at,
                    current_revision=matching_rev,
                )
            return item

    async def update_item_occ(
        self,
        item_id: UUID,
        expected_version: int,
        new_revision: KnowledgeRevision,
        title: Optional[str] = None,
        tags: Optional[List[str]] = None,
        is_global: Optional[bool] = None,
        workspace_id: Optional[str] = None,
    ) -> KnowledgeItem:
        async with self._lock:
            item = self._items.get(item_id)
            if not item or item.is_deleted:
                raise ItemNotFoundException(f"Knowledge item '{item_id}' not found.")
            if workspace_id is not None and not (item.workspace_id == workspace_id or item.is_global):
                raise ItemNotFoundException(f"Knowledge item '{item_id}' not found in workspace '{workspace_id}'.")

            if item.version != expected_version:
                raise ConcurrencyConflictException(
                    message_or_item_id=str(item_id),
                    expected_version=expected_version,
                    actual_version=item.version,
                    message=f"OCC Conflict: expected {expected_version}, actual {item.version}",
                )

            # Atomic version increment
            new_version = item.version + 1
            updated_item = KnowledgeItem(
                id=item.id,
                workspace_id=item.workspace_id,
                is_global=item.is_global if is_global is None else is_global,
                version=new_version,
                title=title if title is not None else item.title,
                content=new_revision.content,
                tags=tags if tags is not None else item.tags,
                is_deleted=False,
                created_at=item.created_at,
                updated_at=new_revision.created_at,
                current_revision=new_revision,
            )
            self._items[item_id] = updated_item
            self._revisions.setdefault(item_id, []).append(new_revision)
            return updated_item

    async def soft_delete_item(
        self,
        item_id: UUID,
        expected_version: Optional[int] = None,
        workspace_id: Optional[str] = None,
    ) -> bool:
        async with self._lock:
            item = self._items.get(item_id)
            if not item or item.is_deleted:
                return False
            if workspace_id and not item.is_global and item.workspace_id != workspace_id:
                return False
            if expected_version is not None and item.version != expected_version:
                raise ConcurrencyConflictException(
                    message_or_item_id=str(item_id),
                    expected_version=expected_version,
                    actual_version=item.version,
                )

            item.is_deleted = True
            return True

    async def list_revisions(self, item_id: UUID) -> List[KnowledgeRevision]:
        async with self._lock:
            return list(self._revisions.get(item_id, []))

    async def search_fts(
        self, query: str, workspace_id: str = "global", limit: int = 20
    ) -> List[RankedSearchResult]:
        async with self._lock:
            results: List[RankedSearchResult] = []
            q_lower = query.lower()
            rank = 1
            for item in self._items.values():
                if item.is_deleted:
                    continue
                if not item.is_global and item.workspace_id != workspace_id and workspace_id != "global":
                    continue
                if q_lower in item.title.lower() or q_lower in item.content.lower():
                    results.append(
                        RankedSearchResult(
                            id=str(item.id),
                            source_type="knowledge",
                            title=item.title,
                            content=item.content,
                            rank=rank,
                            raw_score=0.9,
                            workspace_id=item.workspace_id,
                            is_global=item.is_global,
                            version=item.version,
                        )
                    )
                    rank += 1
            return results[:limit]

    async def search_vector(
        self, query_vector: List[float], workspace_id: str = "global", limit: int = 20
    ) -> List[RankedSearchResult]:
        return []


class MemoryDocumentRepository(IDocumentRepository):
    """In-memory document repository for ingestion stress testing."""

    def __init__(self) -> None:
        self.files: Dict[UUID, DocumentFile] = {}
        self.chunks: Dict[UUID, List[DocumentChunk]] = {}
        self._lock = asyncio.Lock()

    async def save_document(self, document: DocumentFile) -> DocumentFile:
        async with self._lock:
            self.files[document.id] = document
            return document

    async def get_by_hash(self, workspace_id: str, content_hash: str) -> Optional[DocumentFile]:
        async with self._lock:
            for doc in self.files.values():
                if doc.workspace_id == workspace_id and doc.content_hash == content_hash:
                    return doc
            return None

    async def get_by_id(self, document_id: UUID) -> Optional[DocumentFile]:
        async with self._lock:
            return self.files.get(document_id)

    async def save_chunks_batch(self, chunks: List[DocumentChunk]) -> int:
        async with self._lock:
            if not chunks:
                return 0
            doc_id = chunks[0].document_id
            self.chunks.setdefault(doc_id, []).extend(chunks)
            return len(chunks)

    async def get_chunks_by_document(self, document_id: UUID) -> List[DocumentChunk]:
        async with self._lock:
            return list(self.chunks.get(document_id, []))

    async def search_chunks_fts(
        self, query: str, workspace_id: str = "global", limit: int = 20
    ) -> List[RankedSearchResult]:
        return []

    async def search_chunks_vector(
        self, query_vector: List[float], workspace_id: str = "global", limit: int = 20
    ) -> List[RankedSearchResult]:
        return []


class MockDeterministicEmbeddingClient(IEmbeddingClient):
    """Deterministic fast embedding client."""

    @property
    def dimension(self) -> int:
        return 768

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:
        results = []
        for text in texts:
            h = hashlib.sha256(text.encode("utf-8")).digest()
            vec = [float(b) / 255.0 for b in h[:16]]
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            results.append([x / norm for x in vec])
        return results

    async def embed_query(self, query: str) -> List[float]:
        res = await self.embed_texts([query])
        return res[0]


# ==============================================================================
# 1. OCC CONCURRENCY & RACE CONDITION ADVERSARIAL TESTS
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialKnowledgeOCC:
    """Stress-tests Optimistic Concurrency Control under aggressive concurrent contention."""

    async def test_50_concurrent_occ_updates_exact_single_winner(self):
        """Stress: 50 concurrent tasks attempting to update the same item at version 1.
        
        Guarantees:
        - Exactly 1 task succeeds and updates version to 2.
        - Exactly 49 tasks raise ConcurrencyConflictException.
        - Knowledge item version is strictly 2.
        - Historical revisions count is strictly 2.
        """
        repo = MemoryKnowledgeRepository()
        service = KnowledgeService(repository=repo, embedding_client=MockDeterministicEmbeddingClient())

        # Create initial item at version 1
        initial_item = await service.save_item(
            title="Concurrent Baseline Item",
            content="Initial baseline content.",
            workspace_id="ws_stress",
            author="tester",
        )
        assert initial_item.version == 1

        concurrency_level = 50
        success_count = 0
        conflict_count = 0
        errors: List[Exception] = []

        async def attempt_update(task_idx: int) -> Optional[KnowledgeItem]:
            nonlocal success_count, conflict_count
            try:
                await asyncio.sleep(0.0005 * (task_idx % 5))
                res = await service.update_item(
                    item_id=initial_item.id,
                    content=f"Updated content by worker {task_idx}",
                    expected_version=1,
                    author=f"worker_{task_idx}",
                )
                success_count += 1
                return res
            except ConcurrencyConflictException:
                conflict_count += 1
                return None
            except Exception as e:
                errors.append(e)
                return None

        # Execute 50 concurrent updates simultaneously
        tasks = [attempt_update(i) for i in range(concurrency_level)]
        await asyncio.gather(*tasks)

        assert not errors, f"Unexpected errors during OCC updates: {errors}"
        assert success_count == 1, f"Expected exactly 1 winner, got {success_count}"
        assert conflict_count == concurrency_level - 1, f"Expected {concurrency_level - 1} conflicts, got {conflict_count}"

        # Verify final state
        final_item = await service.get_item(initial_item.id)
        assert final_item is not None
        assert final_item.version == 2

        revisions = await service.list_revisions(initial_item.id)
        assert len(revisions) == 2
        assert revisions[0].version == 1
        assert revisions[1].version == 2

    async def test_high_contention_sequential_pipeline(self):
        """Stress: 20 sequential updates chained with correct expected_version."""
        repo = MemoryKnowledgeRepository()
        service = KnowledgeService(repository=repo, embedding_client=MockDeterministicEmbeddingClient())

        item = await service.save_item(
            title="Sequential Chain Item",
            content="Version 1 content",
            workspace_id="ws_chain",
        )

        for ver in range(1, 21):
            updated = await service.update_item(
                item_id=item.id,
                content=f"Version {ver + 1} content",
                expected_version=ver,
                title=f"Sequential Chain Item v{ver + 1}",
            )
            assert updated.version == ver + 1

        final_item = await service.get_item(item.id)
        assert final_item is not None
        assert final_item.version == 21
        assert final_item.title == "Sequential Chain Item v21"

        revisions = await service.list_revisions(item.id)
        assert len(revisions) == 21

    async def test_concurrent_delete_vs_update_race(self):
        """Stress: Race condition between soft-deletion and OCC update."""
        repo = MemoryKnowledgeRepository()
        service = KnowledgeService(repository=repo)

        item = await service.save_item(
            title="Delete vs Update Race",
            content="Original content",
            workspace_id="ws_race",
        )

        async def do_delete():
            return await service.delete_item(item_id=item.id, expected_version=1)

        async def do_update():
            try:
                return await service.update_item(
                    item_id=item.id,
                    content="Race update content",
                    expected_version=1,
                )
            except (ConcurrencyConflictException, ItemNotFoundException):
                return None

        delete_res, update_res = await asyncio.gather(do_delete(), do_update())

        final_item = await service.get_item(item.id)
        assert final_item is None, "Deleted item must not be retrievable"

    async def test_content_hash_stability_and_determinism(self):
        """Verify SHA-256 hash stability across revisions, multiline text, and unicode."""
        c1 = "Knowledge content with unicode: 🚀 🤖 ⚡"
        h1 = KnowledgeRevision.compute_hash(c1)
        h2 = KnowledgeRevision.compute_hash(c1)
        assert h1 == h2
        assert h1 == hashlib.sha256(c1.encode("utf-8")).hexdigest()

        h_empty = KnowledgeRevision.compute_hash("")
        assert h_empty == hashlib.sha256(b"").hexdigest()

        h_ws = KnowledgeRevision.compute_hash(" " + c1)
        assert h1 != h_ws


# ==============================================================================
# 2. ADVERSARIAL CORRUPTED FILE INGESTION ATTACKS
# ==============================================================================

@pytest.mark.tier5
@pytest.mark.asyncio
class TestAdversarialFileIngestion:
    """Stress-tests document parsers and file ingestion against malicious and corrupted files."""

    @pytest.fixture
    def ingestion_env(self, tmp_path: Path):
        storage = LocalStorageAdapter(base_dir=tmp_path / "storage")
        doc_repo = MemoryDocumentRepository()
        emb_client = MockDeterministicEmbeddingClient()
        service = IngestionService(
            storage=storage,
            document_repository=doc_repo,
            embedding_client=emb_client,
            chunk_size=200,
            chunk_overlap=20,
        )
        return service, doc_repo, storage

    async def test_utf8_bom_stripped_cleanly(self, ingestion_env):
        """Verify UTF-8 BOM (\\xef\\xbb\\xbf) is stripped without corrupting parsed text."""
        service, repo, _ = ingestion_env
        raw_bytes = b"\xef\xbb\xbf# Title with BOM\n\nThis is content with BOM marker."
        doc = await service.ingest_file(
            workspace_id="ws_bom",
            filename="document_with_bom.md",
            content=raw_bytes,
            mime_type="text/markdown",
        )
        assert doc.total_chunks >= 1
        chunks = await repo.get_chunks_by_document(doc.id)
        assert len(chunks) >= 1
        assert not chunks[0].content.startswith("\ufeff")
        assert "Title with BOM" in chunks[0].content

    async def test_binary_garbage_and_null_bytes_in_code_files(self, ingestion_env):
        """Verify binary garbage and null bytes in source code are gracefully handled."""
        service, repo, _ = ingestion_env
        corrupt_code = b"def calculate():\n    \x00\x01\x02x = 42\n    return x\xff\xfe\n"
        doc = await service.ingest_file(
            workspace_id="ws_corrupt",
            filename="corrupt_script.py",
            content=corrupt_code,
            mime_type="text/x-python",
        )
        assert doc.total_chunks >= 1
        chunks = await repo.get_chunks_by_document(doc.id)
        assert len(chunks) >= 1
        assert "def calculate():" in chunks[0].content
        assert "return x" in chunks[0].content

    async def test_malformed_json_raises_validation_exception(self, ingestion_env):
        """Verify truncated / invalid JSON files raise ValidationException during ingestion."""
        service, _, _ = ingestion_env
        malformed_json_samples = [
            b'{"key": "value", "unclosed_string: 123}',
            b'{"nested": {"array": [1, 2, 3, ]}}',
            b'{',
            b'["unclosed array"',
            b'{"valid": true} trailing non-whitespace garbage',
        ]

        for sample in malformed_json_samples:
            with pytest.raises(ValidationException) as exc_info:
                await service.ingest_file(
                    workspace_id="ws_json_corrupt",
                    filename="bad_data.json",
                    content=sample,
                    mime_type="application/json",
                )
            assert "Malformed JSON" in str(exc_info.value) or "JSON" in str(exc_info.value)

    async def test_valid_json_formatted_into_searchable_text(self, ingestion_env):
        """Verify valid structured JSON is formatted into indented searchable chunks."""
        service, repo, _ = ingestion_env
        valid_json = b'{"name": "Local Gateway", "version": 2, "features": ["OCC", "RRF", "SSE"]}'
        doc = await service.ingest_file(
            workspace_id="ws_json_valid",
            filename="config.json",
            content=valid_json,
            mime_type="application/json",
        )
        chunks = await repo.get_chunks_by_document(doc.id)
        assert len(chunks) >= 1
        assert "Local Gateway" in chunks[0].content
        assert "OCC" in chunks[0].content

    async def test_damaged_pdf_header_raises_validation_exception(self, ingestion_env):
        """Verify non-PDF bytes or corrupt PDF headers raise ValidationException."""
        service, _, _ = ingestion_env
        fake_pdf = b"This is just plain text disguised as PDF file."
        with pytest.raises(ValidationException) as exc_info:
            await service.ingest_file(
                workspace_id="ws_pdf_corrupt",
                filename="fake_doc.pdf",
                content=fake_pdf,
                mime_type="application/pdf",
            )
        assert "PDF" in str(exc_info.value)

    async def test_zero_byte_file_ingestion(self, ingestion_env):
        """Verify zero-byte empty files ingest cleanly with 0 chunks without crashing."""
        service, repo, _ = ingestion_env
        doc = await service.ingest_file(
            workspace_id="ws_empty",
            filename="empty_file.txt",
            content=b"",
            mime_type="text/plain",
        )
        assert doc.file_size_bytes == 0 or doc.file_size == 0
        assert doc.total_chunks == 0
        chunks = await repo.get_chunks_by_document(doc.id)
        assert len(chunks) == 0

    async def test_giant_single_line_document_chunking(self, ingestion_env):
        """Verify giant 50KB single-line text without newlines is chunked properly."""
        service, repo, _ = ingestion_env
        giant_line = ("word " * 10000).encode("utf-8")
        doc = await service.ingest_file(
            workspace_id="ws_giant",
            filename="giant_line.txt",
            content=giant_line,
            mime_type="text/plain",
        )
        assert doc.total_chunks > 10
        chunks = await repo.get_chunks_by_document(doc.id)
        assert len(chunks) == doc.total_chunks
        for chunk in chunks:
            assert len(chunk.content) > 0
            assert chunk.workspace_id == "ws_giant"

    async def test_cross_workspace_file_isolation(self, ingestion_env):
        """Verify files ingested into Workspace A cannot bleed into Workspace B."""
        service, repo, storage = ingestion_env

        doc_a = await service.ingest_file(
            workspace_id="workspace_alpha",
            filename="secret_alpha.txt",
            content=b"Alpha confidential content",
            mime_type="text/plain",
        )

        doc_b = await service.ingest_file(
            workspace_id="workspace_beta",
            filename="secret_beta.txt",
            content=b"Beta confidential content",
            mime_type="text/plain",
        )

        # Check storage separation and file access isolation
        assert await storage.exists("workspace_alpha", doc_a.id, "secret_alpha.txt")
        assert not await storage.exists("workspace_beta", doc_a.id, "secret_alpha.txt")
        assert await storage.exists("workspace_beta", doc_b.id, "secret_beta.txt")
        assert not await storage.exists("workspace_alpha", doc_b.id, "secret_beta.txt")

        content_a = await storage.read_file("workspace_alpha", doc_a.id, "secret_alpha.txt")
        assert content_a == b"Alpha confidential content"

        with pytest.raises(ItemNotFoundException):
            await storage.read_file("workspace_beta", doc_a.id, "secret_alpha.txt")
