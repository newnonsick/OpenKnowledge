"""Tier 1 Feature Tests for Feature 15: Knowledge Tool Schemas.

Validates tool definitions and JSON schemas exposed to the LLM:
`knowledge_search`, `knowledge_get`, `knowledge_save`, `knowledge_update`, and `knowledge_delete`,
including parameter specifications, required fields, and tool routing helpers.
"""

import json
import pytest

from src.gateway.domain.tools import (
    FunctionCall,
    FunctionDefinition,
    INTERNAL_TOOL_NAMES,
    INTERNAL_TOOL_SCHEMAS,
    KNOWLEDGE_DELETE_SCHEMA,
    KNOWLEDGE_GET_SCHEMA,
    KNOWLEDGE_SAVE_SCHEMA,
    KNOWLEDGE_SEARCH_SCHEMA,
    KNOWLEDGE_UPDATE_SCHEMA,
    ToolCall,
    ToolDefinition,
    ToolResult,
    get_internal_tool_definitions,
    is_internal_tool,
)


@pytest.mark.tier1
@pytest.mark.feature("F15")
def test_f15_all_five_internal_tool_schemas_registered():
    """Verify that all 5 internal knowledge tool schemas are registered and match INTERNAL_TOOL_NAMES."""
    assert len(INTERNAL_TOOL_SCHEMAS) == 5
    schema_names = {s["function"]["name"] for s in INTERNAL_TOOL_SCHEMAS}
    expected_names = {
        "knowledge_search",
        "knowledge_get",
        "knowledge_save",
        "knowledge_update",
        "knowledge_delete",
    }
    assert schema_names == expected_names
    assert INTERNAL_TOOL_NAMES == frozenset(expected_names)


@pytest.mark.tier1
@pytest.mark.feature("F15")
def test_f15_knowledge_search_schema_parameters():
    """Verify knowledge_search schema properties, types, default values, and required query parameter."""
    fn = KNOWLEDGE_SEARCH_SCHEMA["function"]
    assert fn["name"] == "knowledge_search"
    assert "hybrid" in fn["description"].lower() or "search" in fn["description"].lower()

    params = fn["parameters"]
    assert params["type"] == "object"
    props = params["properties"]
    assert "query" in props
    assert props["query"]["type"] == "string"
    assert "workspace_id" in props
    assert "limit" in props
    assert props["limit"]["type"] == "integer"
    assert props["limit"]["default"] == 5
    assert "tags" in props
    assert props["tags"]["type"] == "array"

    assert params["required"] == ["query"]


@pytest.mark.tier1
@pytest.mark.feature("F15")
def test_f15_knowledge_save_and_update_schemas():
    """Verify parameter requirements for knowledge_save and knowledge_update tools."""
    # 1. knowledge_save
    save_fn = KNOWLEDGE_SAVE_SCHEMA["function"]
    assert save_fn["name"] == "knowledge_save"
    save_params = save_fn["parameters"]
    assert set(save_params["required"]) == {"title", "content"}
    assert save_params["properties"]["title"]["type"] == "string"
    assert save_params["properties"]["content"]["type"] == "string"
    assert save_params["properties"]["is_global"]["type"] == "boolean"

    # 2. knowledge_update
    update_fn = KNOWLEDGE_UPDATE_SCHEMA["function"]
    assert update_fn["name"] == "knowledge_update"
    update_params = update_fn["parameters"]
    assert set(update_params["required"]) == {"item_id", "expected_version", "content"}
    assert update_params["properties"]["item_id"]["type"] == "string"
    assert update_params["properties"]["expected_version"]["type"] == "integer"
    assert update_params["properties"]["content"]["type"] == "string"


@pytest.mark.tier1
@pytest.mark.feature("F15")
def test_f15_knowledge_get_and_delete_schemas():
    """Verify parameter requirements for knowledge_get and knowledge_delete tools."""
    # 1. knowledge_get
    get_fn = KNOWLEDGE_GET_SCHEMA["function"]
    assert get_fn["name"] == "knowledge_get"
    assert get_fn["parameters"]["required"] == ["item_id"]
    assert "version" in get_fn["parameters"]["properties"]
    assert get_fn["parameters"]["properties"]["version"]["type"] == "integer"

    # 2. knowledge_delete
    del_fn = KNOWLEDGE_DELETE_SCHEMA["function"]
    assert del_fn["name"] == "knowledge_delete"
    assert del_fn["parameters"]["required"] == ["item_id"]
    assert "expected_version" in del_fn["parameters"]["properties"]


@pytest.mark.tier1
@pytest.mark.feature("F15")
def test_f15_is_internal_tool_helper_and_definitions():
    """Verify is_internal_tool routing helper and Pydantic ToolDefinition generation."""
    # Internal tools return True
    for name in INTERNAL_TOOL_NAMES:
        assert is_internal_tool(name) is True

    # External harness tools return False
    external_tools = ["bash", "edit_file", "git", "mcp__get_data", "run_terminal"]
    for ext_name in external_tools:
        assert is_internal_tool(ext_name) is False

    # Verify Pydantic ToolDefinition object generation
    definitions = get_internal_tool_definitions()
    assert len(definitions) == 5
    assert all(isinstance(d, ToolDefinition) for d in definitions)
    assert {d.function.name for d in definitions} == INTERNAL_TOOL_NAMES
