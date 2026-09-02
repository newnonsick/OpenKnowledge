"""Unit tests for domain entities, canonical models, and exception hierarchy."""

import hashlib
from uuid import uuid4
import pytest
from sqlalchemy import select

from src.gateway.domain.entities import (
    DocumentChunk as DomainDocumentChunk,
    DocumentFile as DomainDocumentFile,
    KnowledgeItem as DomainKnowledgeItem,
    KnowledgeRevision as DomainKnowledgeRevision,
    Workspace as DomainWorkspace,
)
from src.gateway.domain.canonical import (
    CanonicalBlock,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
    BlendedSearchResult,
    RankedSearchResult,
)
from src.gateway.domain.tools import (
    INTERNAL_TOOL_NAMES,
    INTERNAL_TOOL_SCHEMAS,
    ToolCall,
    ToolDefinition,
    ToolResult,
    get_internal_tool_definitions,
    is_internal_tool,
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
from src.gateway.infrastructure.database import normalize_database_url
from src.gateway.infrastructure.persistence.models import (
    Base,
    DocumentChunk,
    DocumentFile,
    KnowledgeItem,
    KnowledgeRevision,
    Workspace,
)
from src.gateway.main import create_app


def test_knowledge_revision_hash():
    """Verify SHA-256 content hashing computation."""
    content = "Sample knowledge content for revision hashing."
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    rev = DomainKnowledgeRevision(
        id=uuid4(),
        item_id=uuid4(),
        version=1,
        content=content,
        content_hash=expected_hash,
        author="agent_1",
    )
    assert rev.content_hash == expected_hash
    assert rev.version == 1
    assert DomainKnowledgeRevision.compute_hash(content) == expected_hash


def test_knowledge_item_defaults():
    """Verify KnowledgeItem default values and properties."""
    item = DomainKnowledgeItem(
        id=uuid4(),
        workspace_id="global",
        title="Architecture Overview",
        content="Clean architecture specifications",
    )
    assert item.version == 1
    assert item.current_version == 1
    assert item.is_deleted is False
    assert item.is_global is False
    assert item.workspace_id == "global"

    item.current_version = 2
    assert item.version == 2


def test_workspace_and_documents():
    """Verify Workspace, DocumentFile, and DocumentChunk construction."""
    ws = DomainWorkspace(id="proj-1", name="Project 1")
    assert ws.id == "proj-1"
    assert ws.name == "Project 1"

    doc = DomainDocumentFile(
        workspace_id=ws.id,
        filename="readme.md",
        file_path="/data/storage/proj-1/readme.md",
        file_size_bytes=1024,
        content_hash="abc123hash",
        mime_type="text/markdown",
    )
    assert doc.filename == "readme.md"
    assert doc.file_size == 1024
    assert doc.is_deleted is False

    # Also test constructor with file_size
    doc2 = DomainDocumentFile(
        workspace_id=ws.id,
        filename="notes.txt",
        file_path="/data/storage/proj-1/notes.txt",
        file_size=2048,
        content_hash="def456hash",
    )
    assert doc2.file_size_bytes == 2048

    chunk = DomainDocumentChunk(
        document_id=doc.id,
        workspace_id=ws.id,
        chunk_index=0,
        content="Section 1 content",
        content_hash="chunkhash0",
        metadata={"section": 1},
    )
    assert chunk.chunk_index == 0
    assert chunk.metadata["section"] == 1


def test_canonical_message_and_request():
    """Verify CanonicalChatRequest and CanonicalMessage construction."""
    msg = CanonicalMessage(role="user", content="Hello AI")
    assert len(msg.content) == 1
    assert isinstance(msg.content[0], CanonicalTextBlock)
    assert msg.text_content == "Hello AI"

    # Test dictionary content conversion
    msg_dict = CanonicalMessage(
        role="assistant",
        content={"type": "text", "text": "Direct dict text"},  # type: ignore
    )
    assert msg_dict.text_content == "Direct dict text"

    req = CanonicalChatRequest(
        model="coding",
        messages=[msg],
        stream=False,
    )
    assert req.model == "coding"
    assert len(req.messages) == 1
    assert req.messages[0].role == "user"


def test_canonical_blocks_and_tools():
    """Verify canonical blocks (text, tool_use, tool_result) and message helpers."""
    text_block = CanonicalTextBlock(text="Let me check that.")
    tool_use_block = CanonicalToolUseBlock(
        id="call_1",
        name="knowledge_search",
        input={"query": "architecture"},
    )
    tool_res_block = CanonicalToolResultBlock(
        tool_use_id="call_1",
        content="Search result content...",
        is_error=False,
    )

    msg = CanonicalMessage(
        role="assistant",
        content=[text_block, tool_use_block, tool_res_block],
    )
    assert msg.text_content == "Let me check that."
    assert len(msg.tool_uses) == 1
    assert msg.tool_uses[0].name == "knowledge_search"
    assert len(msg.tool_results) == 1
    assert msg.tool_results[0].tool_use_id == "call_1"


def test_internal_tools_definitions():
    """Verify internal tool definition loading and checker."""
    definitions = get_internal_tool_definitions()
    assert len(definitions) == 5
    tool_names = {d.function.name for d in definitions}
    assert "knowledge_search" in tool_names
    assert "knowledge_get" in tool_names
    assert "knowledge_save" in tool_names
    assert "knowledge_update" in tool_names
    assert "knowledge_delete" in tool_names

    assert is_internal_tool("knowledge_search") is True
    assert is_internal_tool("knowledge_get") is True
    assert is_internal_tool("unknown_external_tool") is False


def test_exception_hierarchy():
    """Verify all domain exceptions inherit from GatewayException and have proper status codes."""
    exceptions = [
        (ConcurrencyConflictException("Conflict"), 409),
        (ConcurrencyConflictException(message_or_item_id="item-123", expected_version=1, actual_version=2), 409),
        (ItemNotFoundException("Not found"), 404),
        (AuthenticationException("Unauthorized"), 401),
        (ModelNotFoundException("Model not found"), 404),
        (ModelNotFoundException("coding"), 404),
        (ToolExecutionException("Tool error"), 500),
        (ToolExecutionException("knowledge_search", "DB timeout"), 500),
        (StorageException("Storage error"), 500),
        (ValidationException("Invalid payload"), 422),
        (EmbeddingException("Embedding service offline"), 502),
        (LLMProviderException("LLM backend timeout"), 502),
    ]
    for exc, expected_code in exceptions:
        assert isinstance(exc, GatewayException)
        assert exc.status_code == expected_code
        err_dict = exc.to_dict()
        assert "message" in err_dict
        assert "type" in err_dict
        assert "code" in err_dict


def test_database_url_normalization():
    """Verify normalize_database_url converts postgresql:// and postgres:// to asyncpg."""
    assert normalize_database_url("postgresql://user:pass@localhost:5432/db") == "postgresql+asyncpg://user:pass@localhost:5432/db"
    assert normalize_database_url("postgres://user:pass@localhost:5432/db") == "postgresql+asyncpg://user:pass@localhost:5432/db"
    assert normalize_database_url("postgresql+asyncpg://user:pass@localhost:5432/db") == "postgresql+asyncpg://user:pass@localhost:5432/db"


def test_orm_models_instantiation():
    """Verify SQLAlchemy ORM models can be instantiated and mapped correctly."""
    ws = Workspace(id="global", name="Global Workspace")
    assert ws.__tablename__ == "workspaces"
    assert ws.id == "global"

    item_id = uuid4()
    rev_id = uuid4()
    item = KnowledgeItem(
        id=item_id,
        workspace_id="global",
        title="Test Item",
        content="Test Content",
        current_revision_id=rev_id,
    )
    assert item.__tablename__ == "knowledge_items"
    assert item.title == "Test Item"

    rev = KnowledgeRevision(
        id=rev_id,
        item_id=item_id,
        version=1,
        content_hash="testhash",
        content="Test Content",
        author="tester",
    )
    assert rev.__tablename__ == "knowledge_revisions"

    doc = DocumentFile(
        id=uuid4(),
        workspace_id="global",
        filename="test.pdf",
        file_path="/tmp/test.pdf",
        file_size=5000,
        mime_type="application/pdf",
    )
    assert doc.__tablename__ == "document_files"

    chunk = DocumentChunk(
        id=uuid4(),
        document_id=doc.id,
        workspace_id="global",
        chunk_index=0,
        content="Chunk 0 text",
        metadata_={"page": 1},
    )
    assert chunk.__tablename__ == "document_chunks"


def test_fastapi_app_initialization():
    """Verify FastAPI application instance creation and routes."""
    app = create_app()
    assert app.title == "OpenKnowledge"
    paths = []
    for route in app.routes:
        if hasattr(route, "path"):
            paths.append(route.path)
        elif hasattr(route, "original_router") and hasattr(route.original_router, "routes"):
            for r in route.original_router.routes:
                if hasattr(r, "path"):
                    paths.append(r.path)
    assert "/health" in paths


