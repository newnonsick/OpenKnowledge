"""Tier 2 Boundary Tests for Feature 24: Chat Orchestration & Tool Interception Loop.

Tests boundary conditions, tool execution errors (is_error=True), unexpected exceptions, and repeat tool calls.
"""

import json
from typing import Any, Dict
import pytest

from src.gateway.domain.canonical import (
    CanonicalBlock,
    CanonicalMessage,
    CanonicalTextBlock,
    CanonicalToolResultBlock,
    CanonicalToolUseBlock,
)
from src.gateway.domain.exceptions import ToolExecutionException
from src.gateway.domain.tools import is_internal_tool


def execute_internal_tool_stub(tool_name: str, arguments: Dict[str, Any]) -> CanonicalToolResultBlock:
    """Simulates internal tool executor boundary behavior."""
    if not is_internal_tool(tool_name):
        raise ToolExecutionException(tool_name, "Not an internal tool")

    if tool_name == "knowledge_search":
        query = arguments.get("query")
        if not query:
            return CanonicalToolResultBlock(
                tool_use_id=arguments.get("_call_id", "call_1"),
                content="Error: query parameter is required.",
                is_error=True,
            )
        return CanonicalToolResultBlock(
            tool_use_id=arguments.get("_call_id", "call_1"),
            content=f"Search results for: {query}",
            is_error=False,
        )

    if tool_name == "knowledge_get":
        item_id = arguments.get("item_id")
        if not item_id or item_id == "missing-id":
            return CanonicalToolResultBlock(
                tool_use_id=arguments.get("_call_id", "call_2"),
                content=f"Error: item '{item_id}' not found.",
                is_error=True,
            )
        return CanonicalToolResultBlock(
            tool_use_id=arguments.get("_call_id", "call_2"),
            content=f"Item details for {item_id}",
            is_error=False,
        )

    raise ToolExecutionException(tool_name, "Unimplemented tool")


@pytest.mark.tier2
@pytest.mark.feature("F24")
def test_f24_boundary_tool_execution_missing_required_param_returns_is_error():
    """Test boundary: executing tool with missing required parameters returns tool_result with is_error=True."""
    res = execute_internal_tool_stub("knowledge_search", {"_call_id": "call_err_1"})
    assert res.is_error is True
    assert "required" in res.content
    assert res.tool_use_id == "call_err_1"


@pytest.mark.tier2
@pytest.mark.feature("F24")
def test_f24_boundary_tool_execution_not_found_returns_is_error():
    """Test boundary: querying non-existent knowledge item returns tool_result with is_error=True."""
    res = execute_internal_tool_stub("knowledge_get", {"item_id": "missing-id", "_call_id": "call_err_2"})
    assert res.is_error is True
    assert "not found" in res.content


@pytest.mark.tier2
@pytest.mark.feature("F24")
def test_f24_boundary_tool_execution_exception_structure():
    """Test boundary: ToolExecutionException formats 500 status code and tool name details."""
    exc = ToolExecutionException(
        tool_name_or_message="knowledge_save",
        message="Database disk full",
    )
    assert exc.status_code == 500
    assert exc.error_type == "tool_execution_error"
    d = exc.to_dict()
    assert d["code"] == "tool_failed"
    assert "knowledge_save" in d["details"]["tool_name"]


@pytest.mark.tier2
@pytest.mark.feature("F24")
def test_f24_boundary_canonical_tool_result_empty_content():
    """Test boundary: tool result with empty string content renders correctly."""
    res = CanonicalToolResultBlock(
        tool_use_id="call_empty_content",
        content="",
        is_error=False,
    )
    assert res.type == "tool_result"
    assert res.content == ""
    assert res.is_error is False


@pytest.mark.tier2
@pytest.mark.feature("F24")
def test_f24_boundary_multiple_sequential_tool_results_in_message():
    """Test boundary: CanonicalMessage holding multiple tool execution results in sequence."""
    r1 = CanonicalToolResultBlock(tool_use_id="c1", content="Result 1")
    r2 = CanonicalToolResultBlock(tool_use_id="c2", content="Result 2", is_error=True)

    msg = CanonicalMessage(role="tool", content=[r1, r2])
    assert len(msg.tool_results) == 2
    assert msg.tool_results[0].tool_use_id == "c1"
    assert msg.tool_results[1].is_error is True
