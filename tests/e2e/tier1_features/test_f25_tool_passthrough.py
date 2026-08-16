"""Tier 1 Feature Tests for Feature 25: External Harness Tool Passthrough.

Validates seamless passthrough of external harness tools (`bash`, `edit_file`, `git`, `mcp`)
directly to external clients (Cursor, Continue, Roo Code, Cline) without internal execution.
"""

from typing import List, Tuple
import pytest

from src.gateway.domain.canonical import CanonicalChatResponse, CanonicalToolUseBlock
from src.gateway.domain.tools import FunctionCall, ToolCall, is_internal_tool


def partition_tools(tool_calls: List[ToolCall]) -> Tuple[List[ToolCall], List[ToolCall]]:
    """Splits tool calls into internal gateway tools (intercepted) and external harness tools (passed through)."""
    internal = []
    external = []
    for tc in tool_calls:
        if is_internal_tool(tc.function.name):
            internal.append(tc)
        else:
            external.append(tc)
    return internal, external


@pytest.mark.tier1
@pytest.mark.feature("F25")
def test_f25_external_tool_bash_passthrough():
    """Verify external 'bash' tool call is recognized as external and passed through to client."""
    tc = ToolCall(
        id="call_bash_1",
        function=FunctionCall(name="bash", arguments='{"command": "pytest tests/ -v"}'),
    )
    assert is_internal_tool(tc.function.name) is False

    internal, external = partition_tools([tc])
    assert len(internal) == 0
    assert len(external) == 1
    assert external[0].function.name == "bash"
    assert external[0].id == "call_bash_1"


@pytest.mark.tier1
@pytest.mark.feature("F25")
def test_f25_external_tool_edit_file_passthrough():
    """Verify external 'edit_file' tool call with arguments is preserved and passed through."""
    tc = ToolCall(
        id="call_edit_1",
        function=FunctionCall(
            name="edit_file",
            arguments='{"target_file": "src/gateway/config.py", "content": "# Updated"}',
        ),
    )
    internal, external = partition_tools([tc])
    assert len(internal) == 0
    assert len(external) == 1
    assert external[0].function.name == "edit_file"


@pytest.mark.tier1
@pytest.mark.feature("F25")
def test_f25_multiple_external_tools_passthrough():
    """Verify multiple external harness tool calls in a single completion are all retained in order."""
    tc_git = ToolCall(id="call_git", function=FunctionCall(name="git", arguments='{"action": "status"}'))
    tc_bash = ToolCall(id="call_bash", function=FunctionCall(name="bash", arguments='{"command": "ls -la"}'))

    internal, external = partition_tools([tc_git, tc_bash])
    assert len(internal) == 0
    assert len(external) == 2
    assert [e.function.name for e in external] == ["git", "bash"]


@pytest.mark.tier1
@pytest.mark.feature("F25")
def test_f25_mixed_internal_and_external_partitioning():
    """Verify correct partitioning when response contains both internal knowledge tools and external harness tools."""
    tc_internal = ToolCall(
        id="call_search",
        function=FunctionCall(name="knowledge_search", arguments='{"query": "API specs"}'),
    )
    tc_external = ToolCall(
        id="call_bash",
        function=FunctionCall(name="bash", arguments='{"command": "cat specs.md"}'),
    )

    internal, external = partition_tools([tc_internal, tc_external])
    assert len(internal) == 1
    assert internal[0].function.name == "knowledge_search"
    assert len(external) == 1
    assert external[0].function.name == "bash"


@pytest.mark.tier1
@pytest.mark.feature("F25")
def test_f25_custom_mcp_tool_passthrough():
    """Verify custom Model Context Protocol (MCP) tools are passed through transparently."""
    tc_mcp = ToolCall(
        id="call_mcp_1",
        function=FunctionCall(
            name="mcp__postgres_query",
            arguments='{"sql": "SELECT count(*) FROM users;"}',
        ),
    )
    assert is_internal_tool(tc_mcp.function.name) is False
    internal, external = partition_tools([tc_mcp])
    assert len(internal) == 0
    assert len(external) == 1
    assert external[0].function.name == "mcp__postgres_query"
