"""Adversarial stress testing suite for Milestone M1 (Core Foundation & Infrastructure).

Empirically challenges:
1. Optimistic Concurrency Control (OCC) versioning & high-concurrency race simulations.
2. Canonical domain models with extreme payloads, Unicode, null bytes, and deep nesting.
3. Full serialization and deserialization cycles (Pydantic model_dump / model_validate_json).
4. Internal tool schemas, parameter contracts, and function definitions.
5. Domain exception hierarchy, status codes, and leak protection.
6. Local storage adapter under path traversal attacks, Windows reserved filenames, and concurrent I/O stress.
7. Config parsing resilience with malformed inputs and extreme values.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List
from uuid import UUID, uuid4
import pytest
from pydantic import ValidationError

from src.gateway.config import (
    AppSettings,
    DatabaseSettings,
    EmbeddingSettings,
    GatewaySettings,
    LLMSettings,
)
from src.gateway.domain.canonical import (
    BlendedSearchResult,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
    RankedSearchResult,
)
from src.gateway.domain.entities import (
    DocumentChunk,
    DocumentFile,
    KnowledgeItem,
    KnowledgeRevision,
    Workspace,
    utc_now,
)
from src.gateway.domain.exceptions import (
    AuthenticationException,
    ConcurrencyConflictException,
    EmbeddingException,
    GatewayException,
    ItemNotFoundException,
    LLMProviderException,
    ModelNotFoundException,
    StorageException,
    ToolExecutionException,
    ValidationException,
)
from src.gateway.domain.tools import (
    INTERNAL_TOOL_NAMES,
    INTERNAL_TOOL_SCHEMAS,
    FunctionCall,
    FunctionDefinition,
    ToolCall,
    ToolDefinition,
    ToolResult,
    get_internal_tool_definitions,
    is_internal_tool,
)
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter


# ==============================================================================
# 1. OCC Versioning & Concurrency Race Simulation Tests
# ==============================================================================

class TestOCCAndConcurrencySimulation:
    """Stress-tests Optimistic Concurrency Control logic and content hashing."""

    def test_knowledge_revision_content_hashing_determinism(self) -> None:
        """Verify SHA-256 content hashing determinism and collision resilience."""
        content_1 = "Hello world! This is a test document."
        content_2 = "Hello world! This is a test document."
        content_diff = "Hello world! This is a test document. "  # Trailing space

        hash_1 = KnowledgeRevision.compute_hash(content_1)
        hash_2 = KnowledgeRevision.compute_hash(content_2)
        hash_diff = KnowledgeRevision.compute_hash(content_diff)

        assert hash_1 == hash_2
        assert hash_1 == hashlib.sha256(content_1.encode("utf-8")).hexdigest()
        assert hash_1 != hash_diff
        assert len(hash_1) == 64

    def test_knowledge_revision_extreme_and_unicode_hashing(self) -> None:
        """Verify hashing on empty strings, massive 2MB strings, Unicode, and special characters."""
        # Empty string
        empty_hash = KnowledgeRevision.compute_hash("")
        assert empty_hash == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

        # Unicode diverse scripts
        unicode_content = "日本語 简体中文 العربية русский 👨‍👩‍👧‍👦 \u200b\x00\t\r\n"
        unicode_hash = KnowledgeRevision.compute_hash(unicode_content)
        assert len(unicode_hash) == 64
        assert unicode_hash == hashlib.sha256(unicode_content.encode("utf-8")).hexdigest()

        # Large payload (2 MB text)
        large_content = "A" * (2 * 1024 * 1024)
        large_hash = KnowledgeRevision.compute_hash(large_content)
        assert len(large_hash) == 64
        assert large_hash == hashlib.sha256(large_content.encode("utf-8")).hexdigest()

    def test_occ_version_conflict_exception_attributes(self) -> None:
        """Verify ConcurrencyConflictException captures item_id, expected, and actual versions."""
        item_id = str(uuid4())
        exc = ConcurrencyConflictException(
            message_or_item_id=item_id,
            expected_version=3,
            actual_version=4,
        )
        assert exc.status_code == 409
        assert exc.error_type == "concurrency_conflict_error"
        assert exc.code == "version_mismatch"
        assert "expected version 3" in exc.message
        assert "current version is 4" in exc.message
        assert exc.details["item_id"] == item_id
        assert exc.details["expected_version"] == 3
        assert exc.details["actual_version"] == 4

        d = exc.to_dict()
        assert d["type"] == "concurrency_conflict_error"
        assert d["details"]["actual_version"] == 4

    @pytest.mark.asyncio
    async def test_high_concurrency_race_simulation(self) -> None:
        """Simulate a concurrent database update race with 50 competing workers.

        Emulates OCC logic: Each worker attempts to advance version 1 -> 2.
        Only the first worker to acquire the lock/commit succeeds; all others
        must be rejected with ConcurrencyConflictException.
        """
        item = KnowledgeItem(
            id=uuid4(),
            title="Concurrent Spec",
            content="Initial revision",
            version=1,
        )
        lock = asyncio.Lock()
        successes: List[int] = []
        conflicts: List[ConcurrencyConflictException] = []

        async def worker_update(worker_id: int, expected_ver: int) -> None:
            # Simulate slight random latency before attempting commit
            await asyncio.sleep(0.001 * (worker_id % 5))
            async with lock:
                # Simulated atomic OCC check in repository layer
                if item.version != expected_ver:
                    conflicts.append(
                        ConcurrencyConflictException(
                            message_or_item_id=str(item.id),
                            expected_version=expected_ver,
                            actual_version=item.version,
                        )
                    )
                    return

                # Successfully apply update
                item.version += 1
                item.content = f"Updated by worker {worker_id}"
                successes.append(worker_id)

        # 50 workers all attempt to update expecting version 1
        tasks = [worker_update(i, expected_ver=1) for i in range(50)]
        await asyncio.gather(*tasks)

        # Invariant checks
        assert len(successes) == 1, "Exactly one worker must succeed in an OCC race"
        assert len(conflicts) == 49, "All 49 other workers must detect a version conflict"
        assert item.version == 2, "Final version must be 2"

    @pytest.mark.asyncio
    async def test_sequential_occ_version_chaining(self) -> None:
        """Simulate sequential OCC updates where each worker correctly tracks the version."""
        item = KnowledgeItem(
            id=uuid4(),
            title="Chained Spec",
            content="Genesis",
            version=1,
        )
        revisions: List[KnowledgeRevision] = []

        for i in range(1, 26):
            expected_ver = item.version
            new_content = f"Revision content #{i}"
            new_hash = KnowledgeRevision.compute_hash(new_content)
            rev = KnowledgeRevision(
                item_id=item.id,
                version=expected_ver + 1,
                content=new_content,
                content_hash=new_hash,
                author=f"author_{i}",
            )
            revisions.append(rev)
            item.version = rev.version
            item.content = rev.content

        assert item.version == 26
        assert len(revisions) == 25
        assert len({r.content_hash for r in revisions}) == 25  # All hashes distinct


# ==============================================================================
# 2. Canonical Domain Models Extreme Payloads & Type Safety
# ==============================================================================

class TestCanonicalDomainModelsExtremePayloads:
    """Stress-tests canonical models with extreme inputs, Unicode, and edge cases."""

    def test_canonical_text_block_extreme_strings(self) -> None:
        """Test CanonicalTextBlock with 5MB text, emoji surrogate pairs, and zero-width characters."""
        huge_text = "🚀 Unicode test \u200b\u200c\u200d 👨‍👩‍👧‍👦 \x00 " * 50000
        block = CanonicalTextBlock(text=huge_text)
        assert block.type == "text"
        assert block.text == huge_text
        assert len(block.text) > 1_000_000

    def test_canonical_tool_use_block_deep_nesting(self) -> None:
        """Test CanonicalToolUseBlock with deeply nested dictionaries and special characters."""
        nested_input: Dict[str, Any] = {"level": 0}
        curr = nested_input
        for i in range(1, 30):
            curr["nested"] = {"level": i, "data": [i, f"item_{i}"]}
            curr = curr["nested"]

        tool_block = CanonicalToolUseBlock(
            id="call_999_special!@#$",
            name="knowledge_search",
            input=nested_input,
        )
        assert tool_block.type == "tool_use"
        assert tool_block.id == "call_999_special!@#$"
        assert tool_block.input["level"] == 0

    def test_canonical_tool_result_block_polymorphic_content(self) -> None:
        """Test CanonicalToolResultBlock handling both string content and structured list of dicts."""
        # String content
        res1 = CanonicalToolResultBlock(
            tool_use_id="call_1",
            content="Plain error string",
            is_error=True,
        )
        assert res1.content == "Plain error string"
        assert res1.is_error is True

        # Structured list content
        structured_content = [
            {"type": "text", "text": "Result 1"},
            {"type": "image", "source": "data:image/png;base64,..."},
        ]
        res2 = CanonicalToolResultBlock(
            tool_use_id="call_2",
            content=structured_content,
            is_error=False,
        )
        assert res2.content == structured_content
        assert res2.is_error is False

    def test_canonical_message_content_polymorphism_and_properties(self) -> None:
        """Test CanonicalMessage content parsing validator and derived property accessors."""
        # 1. String content auto-converted to [CanonicalTextBlock]
        msg_str = CanonicalMessage(role="user", content="Hello from user string")
        assert len(msg_str.content) == 1
        assert isinstance(msg_str.content[0], CanonicalTextBlock)
        assert msg_str.text_content == "Hello from user string"
        assert msg_str.tool_uses == []
        assert msg_str.tool_results == []

        # 2. Mixed blocks
        t1 = CanonicalTextBlock(text="Calling a tool: ")
        u1 = CanonicalToolUseBlock(id="call_1", name="knowledge_search", input={"query": "test"})
        t2 = CanonicalTextBlock(text="And another tool: ")
        u2 = CanonicalToolUseBlock(id="call_2", name="knowledge_get", input={"item_id": str(uuid4())})
        r1 = CanonicalToolResultBlock(tool_use_id="call_1", content="Found 1 result")

        msg_mixed = CanonicalMessage(
            role="assistant",
            content=[t1, u1, t2, u2, r1],
        )
        assert msg_mixed.text_content == "Calling a tool: And another tool: "
        assert len(msg_mixed.tool_uses) == 2
        assert msg_mixed.tool_uses[0].name == "knowledge_search"
        assert msg_mixed.tool_uses[1].name == "knowledge_get"
        assert len(msg_mixed.tool_results) == 1
        assert msg_mixed.tool_results[0].content == "Found 1 result"

        # 3. Dict input parsed in content list
        msg_dict = CanonicalMessage(
            role="user",
            content=[{"type": "text", "text": "From raw dict"}],  # type: ignore[arg-type]
        )
        assert msg_dict.text_content == "From raw dict"

    def test_canonical_chat_request_validation(self) -> None:
        """Test CanonicalChatRequest constraints and parameter defaults."""
        req = CanonicalChatRequest(
            model="gpt-4o",
            messages=[CanonicalMessage(role="user", content="Hi")],
            temperature=0.0,
            max_tokens=4096,
            stop=["\n\n", "###"],
            workspace_id="ws_123",
        )
        assert req.model == "gpt-4o"
        assert len(req.messages) == 1
        assert req.temperature == 0.0
        assert req.max_tokens == 4096
        assert req.stop == ["\n\n", "###"]
        assert req.workspace_id == "ws_123"

        # Missing required model field
        with pytest.raises(ValidationError):
            CanonicalChatRequest()  # type: ignore[call-arg]

    def test_canonical_chat_response_finish_reasons(self) -> None:
        """Test CanonicalChatResponse accepts all defined finish reasons and rejects invalid ones."""
        valid_reasons = ["stop", "tool_use", "max_tokens", "content_filter", "error"]
        for reason in valid_reasons:
            resp = CanonicalChatResponse(
                id="resp_1",
                model="test-model",
                finish_reason=reason,  # type: ignore[arg-type]
            )
            assert resp.finish_reason == reason

        with pytest.raises(ValidationError):
            CanonicalChatResponse(
                id="resp_2",
                model="test-model",
                finish_reason="unsupported_reason",  # type: ignore[arg-type]
            )

    def test_search_results_domain_models(self) -> None:
        """Test RankedSearchResult and BlendedSearchResult boundary values."""
        ranked = RankedSearchResult(
            id="doc_1",
            source_type="knowledge",
            title="Search Test",
            content="Content snippet",
            rank=1,
            raw_score=0.995,
            workspace_id="ws_abc",
            version=2,
        )
        assert ranked.rank == 1
        assert ranked.raw_score == 0.995

        blended = BlendedSearchResult(
            id="doc_1",
            source_type="document_chunk",
            title="Blended Test",
            content="Blended snippet",
            rrf_score=0.032,
            normalized_score=0.88,
            fts_rank=2,
            vector_rank=1,
            workspace_id="global",
            is_global=True,
        )
        assert blended.fts_rank == 2
        assert blended.vector_rank == 1
        assert blended.is_global is True


# ==============================================================================
# 3. Serialization / Deserialization Round-Trip Cycles
# ==============================================================================

class TestSerializationRoundTrips:
    """Verifies that all domain models survive model_dump / model_validate_json cycles without data loss."""

    def test_workspace_round_trip(self) -> None:
        ws = Workspace(id="ws_roundtrip", name="Roundtrip Workspace")
        json_str = ws.model_dump_json()
        restored = Workspace.model_validate_json(json_str)
        assert restored.id == ws.id
        assert restored.name == ws.name
        assert restored.created_at == ws.created_at

    def test_knowledge_item_and_revision_round_trip(self) -> None:
        item_id = uuid4()
        rev_id = uuid4()
        rev = KnowledgeRevision(
            id=rev_id,
            item_id=item_id,
            version=3,
            title="Rev Title",
            content="Rev Content",
            content_hash=KnowledgeRevision.compute_hash("Rev Content"),
            tags=["tag1", "tag2"],
            author="tester",
        )
        item = KnowledgeItem(
            id=item_id,
            workspace_id="ws_occ",
            is_global=True,
            version=3,
            title="Item Title",
            content="Rev Content",
            tags=["tag1", "tag2"],
            current_revision=rev,
        )

        json_str = item.model_dump_json()
        restored = KnowledgeItem.model_validate_json(json_str)
        assert restored.id == item.id
        assert restored.version == 3
        assert restored.is_global is True
        assert restored.current_revision is not None
        assert restored.current_revision.id == rev_id
        assert restored.current_revision.content_hash == rev.content_hash

    def test_document_file_and_chunk_round_trip(self) -> None:
        doc_id = uuid4()
        doc = DocumentFile(
            id=doc_id,
            workspace_id="ws_doc",
            filename="report.pdf",
            file_path="/storage/ws_doc/report.pdf",
            file_size_bytes=1024,
            mime_type="application/pdf",
            tags=["finance", "q3"],
        )
        assert doc.file_size == 1024  # post-init check

        json_str = doc.model_dump_json()
        restored_doc = DocumentFile.model_validate_json(json_str)
        assert restored_doc.id == doc.id
        assert restored_doc.file_size == 1024
        assert restored_doc.mime_type == "application/pdf"

        chunk = DocumentChunk(
            id=uuid4(),
            document_id=doc_id,
            chunk_index=0,
            content="First chunk text",
            content_hash=hashlib.sha256(b"First chunk text").hexdigest(),
            metadata={"page": 1, "section": "Summary"},
            embedding=[0.1, 0.2, 0.3],
        )
        chunk_json = chunk.model_dump_json()
        restored_chunk = DocumentChunk.model_validate_json(chunk_json)
        assert restored_chunk.document_id == doc_id
        assert restored_chunk.metadata == {"page": 1, "section": "Summary"}
        assert restored_chunk.embedding == [0.1, 0.2, 0.3]

    def test_canonical_chat_request_and_response_round_trip(self) -> None:
        req = CanonicalChatRequest(
            model="claude-3-5-sonnet-20241022",
            messages=[
                CanonicalMessage(
                    role="user",
                    content=[CanonicalTextBlock(text="Perform knowledge search")],
                ),
                CanonicalMessage(
                    role="assistant",
                    content=[
                        CanonicalToolUseBlock(
                            id="tool_1",
                            name="knowledge_search",
                            input={"query": "PostgreSQL"},
                        )
                    ],
                ),
                CanonicalMessage(
                    role="tool",
                    tool_call_id="tool_1",
                    content=[
                        CanonicalToolResultBlock(
                            tool_use_id="tool_1",
                            content="PostgreSQL pgvector support",
                        )
                    ],
                ),
            ],
            tools=get_internal_tool_definitions(),
            temperature=0.2,
        )

        json_str = req.model_dump_json()
        restored_req = CanonicalChatRequest.model_validate_json(json_str)
        assert restored_req.model == req.model
        assert len(restored_req.messages) == 3
        assert len(restored_req.tools) == 5
        assert restored_req.messages[1].tool_uses[0].name == "knowledge_search"

        resp = CanonicalChatResponse(
            id="resp_123",
            model="claude-3-5-sonnet-20241022",
            content=[CanonicalTextBlock(text="Here is your knowledge answer")],
            finish_reason="stop",
            usage=CanonicalUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )
        resp_json = resp.model_dump_json()
        restored_resp = CanonicalChatResponse.model_validate_json(resp_json)
        assert restored_resp.id == "resp_123"
        assert restored_resp.usage.total_tokens == 150


# ==============================================================================
# 4. Internal Tool Schemas & Definitions
# ==============================================================================

class TestInternalToolSchemasAndDefinitions:
    """Validates that all 5 internal knowledge tools adhere to JSON schema contracts."""

    def test_all_five_tool_schemas_present(self) -> None:
        """Verify all 5 tools are defined and have proper names and parameters."""
        expected_tools = {
            "knowledge_search",
            "knowledge_get",
            "knowledge_save",
            "knowledge_update",
            "knowledge_delete",
        }
        assert INTERNAL_TOOL_NAMES == frozenset(expected_tools)
        assert len(INTERNAL_TOOL_SCHEMAS) == 5

        schema_names = {s["function"]["name"] for s in INTERNAL_TOOL_SCHEMAS}
        assert schema_names == expected_tools

    def test_tool_definition_generation(self) -> None:
        """Verify get_internal_tool_definitions creates valid ToolDefinition models."""
        defs = get_internal_tool_definitions()
        assert len(defs) == 5
        for tdef in defs:
            assert isinstance(tdef, ToolDefinition)
            assert tdef.type == "function"
            assert isinstance(tdef.function, FunctionDefinition)
            assert tdef.function.name in INTERNAL_TOOL_NAMES
            assert len(tdef.function.description) > 10
            assert "type" in tdef.function.parameters
            assert tdef.function.parameters["type"] == "object"
            assert "properties" in tdef.function.parameters
            assert "required" in tdef.function.parameters

    def test_is_internal_tool_discriminator(self) -> None:
        """Verify internal tool discrimination under normal and adversarial naming."""
        # Internal tools
        for name in INTERNAL_TOOL_NAMES:
            assert is_internal_tool(name) is True

        # External harness tools - must return False
        external_tools = [
            "bash",
            "edit_file",
            "git",
            "mcp",
            "read_file",
            "run_command",
            "knowledge_search_v2",  # Prefix extension
            "KNOWLEDGE_SEARCH",     # Uppercase
            " knowledge_save ",     # Whitespace
            "",
            "unknown",
        ]
        for name in external_tools:
            assert is_internal_tool(name) is False

    def test_tool_call_and_result_models(self) -> None:
        """Test ToolCall and ToolResult construction and argument parsing."""
        call = ToolCall(
            id="call_abc",
            function=FunctionCall(
                name="knowledge_search",
                arguments=json.dumps({"query": "architecture", "limit": 10}),
            ),
        )
        assert call.id == "call_abc"
        assert call.function.name == "knowledge_search"

        # Verify arguments decode
        args = json.loads(call.function.arguments)
        assert args["query"] == "architecture"
        assert args["limit"] == 10

        res = ToolResult(
            tool_call_id=call.id,
            name=call.function.name,
            content="Search result content",
            is_error=False,
        )
        assert res.tool_call_id == "call_abc"
        assert res.is_error is False


# ==============================================================================
# 5. Domain Exception Hierarchy & Information Leaks
# ==============================================================================

class TestDomainExceptions:
    """Verifies domain exception status codes, serialization, and leak-free payloads."""

    def test_all_exception_status_codes_and_types(self) -> None:
        """Check standard HTTP status codes and error_type codes for all domain exceptions."""
        cases = [
            (GatewayException("Gateway err"), 500, "gateway_error", "gateway_error"),
            (AuthenticationException("Auth err"), 401, "authentication_error", "invalid_api_key"),
            (ItemNotFoundException("Not found"), 404, "not_found_error", "resource_not_found"),
            (ConcurrencyConflictException("Conflict"), 409, "concurrency_conflict_error", "version_mismatch"),
            (ModelNotFoundException("Model err"), 404, "not_found_error", "model_not_found"),
            (ToolExecutionException("Tool err"), 500, "tool_execution_error", "tool_failed"),
            (StorageException("Storage err"), 500, "storage_error", "storage_operation_failed"),
            (ValidationException("Validation err"), 422, "validation_error", "invalid_payload"),
            (EmbeddingException("Embedding err"), 502, "embedding_error", "embedding_provider_error"),
            (LLMProviderException("LLM err"), 502, "llm_provider_error", "llm_service_failed"),
        ]

        for exc, expected_status, expected_type, expected_code in cases:
            assert exc.status_code == expected_status
            assert exc.error_type == expected_type
            assert exc.code == expected_code
            d = exc.to_dict()
            assert d["type"] == expected_type
            assert d["code"] == expected_code
            assert "message" in d

    def test_exception_details_preservation(self) -> None:
        """Verify custom structured details dictionary is cleanly represented."""
        exc = ValidationException(
            message="Payload field 'query' cannot be empty",
            details={"field": "query", "error": "empty_string"},
        )
        d = exc.to_dict()
        assert d["details"] == {"field": "query", "error": "empty_string"}


# ==============================================================================
# 6. Local Storage Adapter Adversarial Stress & Path Traversal
# ==============================================================================

class TestLocalStorageAdapterAdversarial:
    """Stress-tests LocalStorageAdapter with path traversal, Windows reserved names, and concurrent I/O."""

    @pytest.fixture
    def storage(self, tmp_path: Path) -> LocalStorageAdapter:
        return LocalStorageAdapter(base_dir=tmp_path / "storage_root")

    @pytest.mark.asyncio
    async def test_path_traversal_attacks_rejected(self, storage: LocalStorageAdapter) -> None:
        """Attempt directory traversal in workspace_id, file_id, and filename."""
        traversal_workspaces = [
            "../secret",
            "..\\secret",
            "../../etc",
            "workspace/../../escape",
            "ws_!@#$",
            "ws 123",  # Space
            "",
        ]
        for ws in traversal_workspaces:
            with pytest.raises(StorageException):
                await storage.save_file(ws, "file123", "test.txt", b"payload")

        traversal_file_ids = [
            "../evil_id",
            "..\\evil_id",
            "id with spaces",
            "id/with/slashes",
            "id;drop table",
        ]
        for fid in traversal_file_ids:
            with pytest.raises(StorageException):
                await storage.save_file("ws_main", fid, "test.txt", b"payload")

    @pytest.mark.asyncio
    async def test_windows_reserved_and_illegal_characters_sanitized(self, storage: LocalStorageAdapter) -> None:
        """Verify Windows illegal characters in filenames are safely sanitized."""
        raw_filenames = [
            'file<with>quotes"and:colons.txt',
            "file/with/slashes.txt",
            "file\\with\\backslashes.txt",
            "file|pipe?question*star.txt",
            "....leadingdots.txt",
            "   spaces_and_tabs.txt",
        ]
        for fname in raw_filenames:
            saved_path = await storage.save_file("ws_sanitize", "f_san", fname, b"data")
            assert saved_path.exists()
            assert saved_path.is_file()
            assert str(storage.base_dir) in str(saved_path)

    def test_document_file_zero_byte_edge_case(self) -> None:
        """Verify DocumentFile file_size and file_size_bytes behavior on 0-byte file."""
        doc = DocumentFile(
            filename="empty.txt",
            file_path="/data/empty.txt",
            file_size_bytes=0,
        )
        assert doc.file_size_bytes == 0
        assert doc.file_size == 0

    def test_filename_sanitizer_control_chars_sanitized(self, storage: LocalStorageAdapter) -> None:
        """Verify that _sanitize_filename replaces embedded control characters including tabs and newlines."""
        raw_name = "file\twith\nnewlines.txt"
        sanitized = storage._sanitize_filename(raw_name)
        assert "\t" not in sanitized and "\n" not in sanitized
        assert sanitized == "file_with_newlines.txt"



    @pytest.mark.asyncio
    async def test_large_binary_file_round_trip(self, storage: LocalStorageAdapter) -> None:
        """Write and read a 4MB binary payload with random bytes."""
        random_bytes = os.urandom(4 * 1024 * 1024)
        file_id = "f_large_bin"
        filename = "binary_dump.bin"
        workspace = "ws_binary"

        saved_path = await storage.save_file(workspace, file_id, filename, random_bytes)
        assert saved_path.exists()
        assert saved_path.stat().st_size == len(random_bytes)

        read_bytes = await storage.read_file(workspace, file_id, filename)
        assert read_bytes == random_bytes

    @pytest.mark.asyncio
    async def test_concurrent_io_race_safety(self, storage: LocalStorageAdapter) -> None:
        """Execute 30 concurrent file writes and reads across multiple workspaces."""
        async def perform_io(idx: int) -> None:
            ws = f"ws_worker_{idx % 3}"
            fid = f"file_{idx}"
            fname = f"doc_{idx}.txt"
            content = f"Content from worker {idx}".encode("utf-8")

            await storage.save_file(ws, fid, fname, content)
            exists = await storage.exists(ws, fid, fname)
            assert exists is True

            data = await storage.read_file(ws, fid, fname)
            assert data == content

            deleted = await storage.delete_file(ws, fid, fname)
            assert deleted is True

            exists_after = await storage.exists(ws, fid, fname)
            assert exists_after is False

        tasks = [perform_io(i) for i in range(30)]
        await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    async def test_atomic_write_leaves_no_orphan_temp_files(self, storage: LocalStorageAdapter) -> None:
        """Verify atomic writes leave no left-over .tmp files in workspace directory."""
        ws = "ws_atomic"
        for i in range(10):
            await storage.save_file(ws, f"f_{i}", f"file_{i}.txt", b"atomic content")

        ws_dir = storage.base_dir / ws
        assert ws_dir.exists()
        temp_files = list(ws_dir.glob(".tmp_*"))
        assert len(temp_files) == 0, f"Found orphan temp files: {temp_files}"


# ==============================================================================
# 7. Config Settings Adversarial Parsing
# ==============================================================================

class TestConfigSettingsAdversarial:
    """Verifies that config parsers handle malformed lists, spaces, and edge formats."""

    def test_api_keys_malformed_and_edge_inputs(self) -> None:
        """Test GATEWAY_API_KEYS validator with JSON arrays, comma strings, and dirty formatting."""
        # JSON array string with whitespace
        gw1 = GatewaySettings(api_keys='[" key1 ", "key2", "  "]')
        assert gw1.api_keys == ["key1", "key2"]

        # Comma separated string with empty segments
        gw2 = GatewaySettings(api_keys="key_a, , key_b ,, key_c ")
        assert gw2.api_keys == ["key_a", "key_b", "key_c"]

        # Single string key
        gw3 = GatewaySettings(api_keys="single-key")
        assert gw3.api_keys == ["single-key"]

    def test_cors_origins_malformed_and_edge_inputs(self) -> None:
        """Test CORS_ORIGINS validator with JSON arrays, commas, and lists."""
        gw1 = GatewaySettings(cors_origins='["http://localhost:3000", " http://app.local "]')
        assert gw1.cors_origins == ["http://localhost:3000", "http://app.local"]

        gw2 = GatewaySettings(cors_origins="http://a.com, http://b.com , ")
        assert gw2.cors_origins == ["http://a.com", "http://b.com"]

    def test_app_settings_nested_instantiation_resilience(self) -> None:
        """Test AppSettings instantiation with dict sub-settings and extra fields."""
        custom_settings = AppSettings(
            llm={"url": "http://custom-llm:8000", "context_window": 16384},
            embedding={"dimension": 1536, "url": "http://custom-emb:8000"},
            database={"url": "postgresql+asyncpg://user:pass@db:5432/testdb"},
            gateway={"storage_dir": "./custom_storage", "max_tool_iterations": 20},
        )
        assert custom_settings.llm.url == "http://custom-llm:8000"
        assert custom_settings.llm.context_window == 16384
        assert custom_settings.embedding.dimension == 1536
        assert custom_settings.database.url == "postgresql+asyncpg://user:pass@db:5432/testdb"
        assert custom_settings.gateway.max_tool_iterations == 20

    def test_numeric_constraints_validation(self) -> None:
        """Verify that negative and zero values for strictly positive fields raise ValidationError."""
        # LLMSettings: context_window <= 0, timeout_seconds <= 0, max_tokens <= 0
        with pytest.raises(ValidationError):
            LLMSettings(context_window=0)
        with pytest.raises(ValidationError):
            LLMSettings(context_window=-100)
        with pytest.raises(ValidationError):
            LLMSettings(timeout_seconds=0.0)
        with pytest.raises(ValidationError):
            LLMSettings(timeout_seconds=-10.0)
        with pytest.raises(ValidationError):
            LLMSettings(temperature=-0.1)
        with pytest.raises(ValidationError):
            LLMSettings(max_tokens=0)

        # EmbeddingSettings: dimension <= 0, batch_size <= 0, timeout_seconds <= 0
        with pytest.raises(ValidationError):
            EmbeddingSettings(dimension=0)
        with pytest.raises(ValidationError):
            EmbeddingSettings(dimension=-768)
        with pytest.raises(ValidationError):
            EmbeddingSettings(batch_size=0)
        with pytest.raises(ValidationError):
            EmbeddingSettings(timeout_seconds=0.0)

        # DatabaseSettings: pool_size <= 0, max_overflow < 0, pool_timeout <= 0, pool_recycle <= 0
        with pytest.raises(ValidationError):
            DatabaseSettings(pool_size=0)
        with pytest.raises(ValidationError):
            DatabaseSettings(pool_size=-5)
        with pytest.raises(ValidationError):
            DatabaseSettings(max_overflow=-1)
        with pytest.raises(ValidationError):
            DatabaseSettings(pool_timeout=0.0)
        with pytest.raises(ValidationError):
            DatabaseSettings(pool_recycle=0)

        # GatewaySettings: port <= 0 or > 65535, max_tool_iterations <= 0, tool_timeout_seconds <= 0
        with pytest.raises(ValidationError):
            GatewaySettings(port=0)
        with pytest.raises(ValidationError):
            GatewaySettings(port=70000)
        with pytest.raises(ValidationError):
            GatewaySettings(max_tool_iterations=0)
        with pytest.raises(ValidationError):
            GatewaySettings(tool_timeout_seconds=0.0)
