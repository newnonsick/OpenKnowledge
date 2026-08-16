"""Tier 2 Boundary Tests for Feature 15: Knowledge Tool Schemas.

Tests boundary conditions, schema validation, malformed tool arguments, missing required fields, and unknown tools.
"""

import json
import pytest

from src.gateway.domain.tools import (
    FunctionCall,
    INTERNAL_TOOL_NAMES,
    INTERNAL_TOOL_SCHEMAS,
    KNOWLEDGE_GET_SCHEMA,
    KNOWLEDGE_SAVE_SCHEMA,
    KNOWLEDGE_SEARCH_SCHEMA,
    KNOWLEDGE_UPDATE_SCHEMA,
    ToolCall,
    get_internal_tool_definitions,
    is_internal_tool,
)


@pytest.mark.tier2
@pytest.mark.feature("F15")
def test_f15_boundary_all_internal_tools_have_required_fields_and_parameters():
    """Test boundary: every internal tool schema defines name, description, parameters, and required list."""
    assert len(INTERNAL_TOOL_SCHEMAS) == 5
    for tool_schema in INTERNAL_TOOL_SCHEMAS:
        assert tool_schema["type"] == "function"
        fn = tool_schema["function"]
        assert fn["name"] in INTERNAL_TOOL_NAMES
        assert len(fn["description"]) > 10
        assert fn["parameters"]["type"] == "object"
        assert isinstance(fn["parameters"]["required"], list)
        assert len(fn["parameters"]["required"]) >= 1


@pytest.mark.tier2
@pytest.mark.feature("F15")
def test_f15_boundary_is_internal_tool_with_empty_and_unknown_names():
    """Test boundary: is_internal_tool returns False for empty, None, or unknown tool names."""
    assert is_internal_tool("knowledge_search") is True
    assert is_internal_tool("knowledge_save") is True
    assert is_internal_tool("knowledge_get") is True
    assert is_internal_tool("knowledge_update") is True
    assert is_internal_tool("knowledge_delete") is True

    # External harness tools
    assert is_internal_tool("bash") is False
    assert is_internal_tool("edit_file") is False
    assert is_internal_tool("git") is False
    assert is_internal_tool("unknown_tool_xyz") is False
    assert is_internal_tool("") is False


@pytest.mark.tier2
@pytest.mark.feature("F15")
def test_f15_boundary_tool_call_malformed_json_arguments_string():
    """Test boundary: ToolCall handles malformed non-JSON argument strings gracefully."""
    tc = ToolCall(
        id="call_test_123",
        function=FunctionCall(name="knowledge_search", arguments="NOT_A_VALID_JSON_{{"),
    )
    assert tc.id == "call_test_123"
    assert tc.function.name == "knowledge_search"
    assert tc.function.arguments == "NOT_A_VALID_JSON_{{"

    # Parsing as JSON raises JSONDecodeError as expected
    with pytest.raises(json.JSONDecodeError):
        json.loads(tc.function.arguments)


@pytest.mark.tier2
@pytest.mark.feature("F15")
def test_f15_boundary_knowledge_search_schema_required_fields():
    """Test boundary: knowledge_search requires 'query' property."""
    required = KNOWLEDGE_SEARCH_SCHEMA["function"]["parameters"]["required"]
    assert "query" in required
    assert "limit" not in required  # optional with default


@pytest.mark.tier2
@pytest.mark.feature("F15")
def test_f15_boundary_get_internal_tool_definitions_instantiation():
    """Test boundary: get_internal_tool_definitions produces valid Pydantic ToolDefinition instances."""
    definitions = get_internal_tool_definitions()
    assert len(definitions) == 5
    names = {d.function.name for d in definitions}
    assert names == INTERNAL_TOOL_NAMES
