"""Tier 2 Boundary Tests for Feature 2: Pragmatic Clean Architecture Core.

Tests boundary conditions, architectural constraints, layer boundaries, and interface contracts.
"""

import sys
import uuid
import pytest
from pydantic import ValidationError

from src.gateway.domain.canonical import (
    CanonicalChatRequest,
    CanonicalMessage,
    CanonicalTextBlock,
    CanonicalToolUseBlock,
)
from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.domain.exceptions import GatewayException, ItemNotFoundException
from src.gateway.application.ports.clients import IEmbeddingClient, ILLMClient
from src.gateway.application.ports.repositories import IKnowledgeRepository
from src.gateway.application.ports.storage import IFileStorage


@pytest.mark.tier2
@pytest.mark.feature("F2")
def test_f02_boundary_domain_layer_isolation_no_infrastructure_imports():
    """Test boundary: domain module must not import infrastructure or presentation modules."""
    import src.gateway.domain.entities as entities_mod
    import src.gateway.domain.canonical as canonical_mod
    import src.gateway.domain.exceptions as exceptions_mod

    for mod in (entities_mod, canonical_mod, exceptions_mod):
        mod_dict = mod.__dict__
        for name, val in mod_dict.items():
            if isinstance(val, type(sys)):
                module_name = getattr(val, "__name__", "")
                assert "presentation" not in module_name, f"Domain imported presentation: {module_name}"
                assert "infrastructure" not in module_name, f"Domain imported infrastructure: {module_name}"


@pytest.mark.tier2
@pytest.mark.feature("F2")
def test_f02_boundary_non_existent_entity_ids_and_nil_uuid():
    """Test boundary: handling nil UUIDs and non-existent IDs in domain aggregate entities."""
    nil_uuid = uuid.UUID("00000000-0000-0000-0000-000000000000")

    item = KnowledgeItem(id=nil_uuid, title="Nil UUID Item", workspace_id="global")
    assert item.id == nil_uuid
    assert item.version == 1
    assert item.is_deleted is False

    rev = KnowledgeRevision(
        id=nil_uuid,
        item_id=nil_uuid,
        version=1,
        content="Nil UUID content",
        content_hash=KnowledgeRevision.compute_hash("Nil UUID content"),
    )
    assert rev.id == nil_uuid
    assert rev.item_id == nil_uuid


@pytest.mark.tier2
@pytest.mark.feature("F2")
def test_f02_boundary_port_interface_cannot_be_instantiated_directly():
    """Test boundary: abstract port interfaces must enforce method implementation on subclasses."""
    with pytest.raises(TypeError):
        ILLMClient()  # type: ignore

    with pytest.raises(TypeError):
        IEmbeddingClient()  # type: ignore

    with pytest.raises(TypeError):
        IKnowledgeRepository()  # type: ignore

    with pytest.raises(TypeError):
        IFileStorage()  # type: ignore


@pytest.mark.tier2
@pytest.mark.feature("F2")
def test_f02_boundary_canonical_message_empty_and_mixed_content_types():
    """Test boundary: CanonicalMessage validates role, handles empty content, and accepts block lists."""
    # Valid empty content
    msg_empty = CanonicalMessage(role="user", content=[])
    assert msg_empty.content == []
    assert msg_empty.text_content == ""
    assert msg_empty.tool_uses == []

    # Text block + Tool use block combination
    tb = CanonicalTextBlock(text="Please check this tool:")
    tub = CanonicalToolUseBlock(id="call_123", name="knowledge_search", input={"query": "test"})
    msg_mixed = CanonicalMessage(role="assistant", content=[tb, tub])

    assert len(msg_mixed.content) == 2
    assert msg_mixed.text_content == "Please check this tool:"
    assert len(msg_mixed.tool_uses) == 1
    assert msg_mixed.tool_uses[0].name == "knowledge_search"

    # Invalid role raises ValidationError
    with pytest.raises(ValidationError):
        CanonicalMessage(role="invalid_role", content=[])  # type: ignore


@pytest.mark.tier2
@pytest.mark.feature("F2")
def test_f02_boundary_domain_exception_hierarchy_and_serialization():
    """Test boundary: custom domain exceptions serialize standard error dictionaries with details."""
    exc = ItemNotFoundException(
        message="Item with ID missing-123 was not found",
        details={"item_id": "missing-123", "workspace": "custom_ws"},
    )
    assert isinstance(exc, GatewayException)
    assert exc.status_code == 404
    assert exc.error_type == "not_found_error"

    d = exc.to_dict()
    assert d["message"] == "Item with ID missing-123 was not found"
    assert d["type"] == "not_found_error"
    assert d["details"]["item_id"] == "missing-123"
