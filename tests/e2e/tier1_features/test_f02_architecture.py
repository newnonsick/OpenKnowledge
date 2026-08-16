"""Tier 1 Feature Tests for Feature 2: Pragmatic Clean Architecture Core.

Validates domain models, domain exception hierarchy, port interfaces contracts,
and canonical protocol structures.
"""

from abc import ABC
import pytest

from src.gateway.application.ports.clients import IEmbeddingClient, ILLMClient
from src.gateway.application.ports.repositories import (
    IDocumentRepository,
    IKnowledgeRepository,
    IWorkspaceRepository,
)
from src.gateway.application.ports.storage import IFileStorage
from src.gateway.domain.canonical import (
    BlendedSearchResult,
    CanonicalChatRequest,
    CanonicalChatResponse,
    CanonicalMessage,
    CanonicalStreamChunk,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
    CanonicalUsage,
    RankedSearchResult,
)
from src.gateway.domain.exceptions import (
    AuthenticationException,
    ConcurrencyConflictException,
    GatewayException,
    ItemNotFoundException,
    LLMProviderException,
    ModelNotFoundException,
    StorageException,
    ValidationException,
)
from src.gateway.domain.tools import ToolCall, ToolDefinition


@pytest.mark.tier1
@pytest.mark.feature("F2")
def test_f02_base_exception_hierarchy():
    """Verify domain exception hierarchy, HTTP status codes, and serialization dictionary."""
    base_exc = GatewayException("Generic error", status_code=500, error_type="gateway_error")
    assert base_exc.status_code == 500
    assert base_exc.to_dict()["message"] == "Generic error"

    auth_exc = AuthenticationException("Missing Bearer token")
    assert auth_exc.status_code == 401
    assert auth_exc.error_type == "authentication_error"

    not_found = ItemNotFoundException("Knowledge item not found")
    assert not_found.status_code == 404

    conflict = ConcurrencyConflictException(
        message_or_item_id="item-123", expected_version=2, actual_version=3
    )
    assert conflict.status_code == 409
    assert conflict.details["expected_version"] == 2

    val_exc = ValidationException("Invalid prompt")
    assert val_exc.status_code == 422

    llm_exc = LLMProviderException("Connection reset by peer")
    assert llm_exc.status_code == 502


@pytest.mark.tier1
@pytest.mark.feature("F2")
def test_f02_port_interfaces_contracts():
    """Verify application port interfaces are abstract base classes with defined contracts."""
    assert issubclass(ILLMClient, ABC)
    assert issubclass(IEmbeddingClient, ABC)
    assert issubclass(IFileStorage, ABC)
    assert issubclass(IKnowledgeRepository, ABC)
    assert issubclass(IDocumentRepository, ABC)
    assert issubclass(IWorkspaceRepository, ABC)

    # Verify abstract methods exist on interfaces
    assert hasattr(ILLMClient, "generate")
    assert hasattr(ILLMClient, "generate_stream")
    assert hasattr(IEmbeddingClient, "embed_texts")
    assert hasattr(IEmbeddingClient, "embed_query")
    assert hasattr(IFileStorage, "save_file")
    assert hasattr(IFileStorage, "read_file")
    assert hasattr(IKnowledgeRepository, "create_item")
    assert hasattr(IKnowledgeRepository, "update_item_occ")
    assert hasattr(IDocumentRepository, "save_chunks_batch")


@pytest.mark.tier1
@pytest.mark.feature("F2")
def test_f02_canonical_chat_models():
    """Verify protocol-neutral canonical chat request and response models."""
    usage = CanonicalUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    assert usage.total_tokens == 150

    msg = CanonicalMessage(role="user", content=[CanonicalTextBlock(text="Explain Clean Architecture")])
    assert msg.role == "user"
    assert msg.text_content == "Explain Clean Architecture"

    req = CanonicalChatRequest(
        model="coding",
        messages=[msg],
        workspace_id="ws-proj-1",
        temperature=0.3,
        stream=False,
    )
    assert req.model == "coding"
    assert req.workspace_id == "ws-proj-1"

    resp = CanonicalChatResponse(
        id="resp_001",
        model="coding",
        content=[CanonicalTextBlock(text="Clean Architecture organizes code into layers.")],
        finish_reason="stop",
        usage=usage,
    )
    assert resp.id == "resp_001"
    assert resp.finish_reason == "stop"


@pytest.mark.tier1
@pytest.mark.feature("F2")
def test_f02_canonical_message_content_parsing():
    """Verify CanonicalMessage parsing for raw strings, dictionaries, and mixed content blocks."""
    # String input auto-converts to CanonicalTextBlock
    msg_str = CanonicalMessage(role="user", content="Hello world")
    assert len(msg_str.content) == 1
    assert isinstance(msg_str.content[0], CanonicalTextBlock)
    assert msg_str.text_content == "Hello world"

    # Multi-block message with text and tool use
    tool_block = CanonicalToolUseBlock(
        id="tool_1",
        name="knowledge_search",
        input={"query": "database schema"},
    )
    tool_result = CanonicalToolResultBlock(
        tool_use_id="tool_1",
        content="PostgreSQL 16",
        is_error=False,
    )
    msg_mixed = CanonicalMessage(
        role="assistant",
        content=[
            CanonicalTextBlock(text="Searching knowledge..."),
            tool_block,
            tool_result,
        ],
    )
    assert msg_mixed.text_content == "Searching knowledge..."
    assert len(msg_mixed.tool_uses) == 1
    assert msg_mixed.tool_uses[0].name == "knowledge_search"
    assert len(msg_mixed.tool_results) == 1
    assert msg_mixed.tool_results[0].content == "PostgreSQL 16"


@pytest.mark.tier1
@pytest.mark.feature("F2")
def test_f02_canonical_search_result_models():
    """Verify RankedSearchResult and BlendedSearchResult domain entities."""
    ranked = RankedSearchResult(
        id="k-item-01",
        source_type="knowledge",
        title="Architecture Guide",
        content="Clean Architecture details...",
        rank=1,
        raw_score=0.95,
        workspace_id="global",
        is_global=True,
        version=1,
    )
    assert ranked.source_type == "knowledge"
    assert ranked.rank == 1

    blended = BlendedSearchResult(
        id="k-item-01",
        source_type="knowledge",
        title="Architecture Guide",
        content="Clean Architecture details...",
        rrf_score=0.032,
        normalized_score=0.98,
        fts_rank=1,
        vector_rank=2,
        workspace_id="global",
        is_global=True,
    )
    assert blended.rrf_score == 0.032
    assert blended.fts_rank == 1
    assert blended.vector_rank == 2
