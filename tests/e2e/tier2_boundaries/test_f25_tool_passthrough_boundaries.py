"""Tier 2 Boundary Tests for Feature 25: External Harness Tool Passthrough.

Tests boundary conditions, separating internal vs external tools, passthrough fidelity, and special character arguments.
"""

from typing import List, Tuple
import pytest
from src.gateway.domain.canonical import CanonicalToolUseBlock
from src.gateway.domain.tools import ToolCall, is_internal_tool


def partition_tools(
    tool_calls: List[ToolCall],
) -> Tuple[List[ToolCall], List[ToolCall]]:
    """Splits tool calls into internal gateway tools (intercepted) and external harness tools (passed through)."""
    internal = []
    external = []
    for tc in tool_calls:
        if is_internal_tool(tc.function.name):
            internal.append(tc)
        else:
            external.append(tc)
    return internal, external


@pytest.mark.tier2
@pytest.mark.feature("F25")
def test_f25_boundary_mixture_of_internal_and_external_tools():
    """Test boundary: LLM response with both internal (knowledge_search) and external (bash, edit_file) tools."""
    from src.gateway.domain.tools import FunctionCall

    tc_int = ToolCall(
        id="call_int_1",
        function=FunctionCall(name="knowledge_search", arguments='{"query": "api spec"}'),
    )
    tc_ext1 = ToolCall(
        id="call_ext_1",
        function=FunctionCall(name="bash", arguments='{"command": "git status"}'),
    )
    tc_ext2 = ToolCall(
        id="call_ext_2",
        function=FunctionCall(name="edit_file", arguments='{"path": "main.py"}'),
    )

    internal, external = partition_tools([tc_int, tc_ext1, tc_ext2])
    assert len(internal) == 1
    assert internal[0].function.name == "knowledge_search"
    assert len(external) == 2
    assert [e.function.name for e in external] == ["bash", "edit_file"]


@pytest.mark.tier2
@pytest.mark.feature("F25")
def test_f25_boundary_empty_tool_calls_list():
    """Test boundary: empty tool calls list partitions cleanly into two empty lists."""
    internal, external = partition_tools([])
    assert internal == []
    assert external == []


@pytest.mark.tier2
@pytest.mark.feature("F25")
def test_f25_boundary_external_tool_with_special_characters_in_arguments():
    """Test boundary: external tool (e.g. bash command) with quotes, pipes, and backslashes is preserved."""
    from src.gateway.domain.tools import FunctionCall

    complex_command = '{"command": "grep -rn \\"TODO: fix\\" . | awk \'{print $1}\' > output.log"}'
    tc_bash = ToolCall(
        id="call_bash_complex",
        function=FunctionCall(name="bash", arguments=complex_command),
    )
    internal, external = partition_tools([tc_bash])
    assert len(internal) == 0
    assert len(external) == 1
    assert external[0].function.arguments == complex_command


@pytest.mark.tier2
@pytest.mark.feature("F25")
def test_f25_boundary_all_external_tools_zero_internal():
    """Test boundary: response containing only external tools (mcp, git, bash) leaves internal list empty."""
    from src.gateway.domain.tools import FunctionCall

    tc_git = ToolCall(id="c1", function=FunctionCall(name="git", arguments='{"action": "diff"}'))
    tc_mcp = ToolCall(id="c2", function=FunctionCall(name="mcp__weather", arguments='{"city": "Tokyo"}'))

    internal, external = partition_tools([tc_git, tc_mcp])
    assert len(internal) == 0
    assert len(external) == 2


@pytest.mark.tier2
@pytest.mark.feature("F25")
def test_f25_boundary_unknown_custom_tool_passthrough():
    """Test boundary: completely unknown custom harness tool names are safely treated as external passthrough."""
    from src.gateway.domain.tools import FunctionCall

    tc_custom = ToolCall(id="c_custom", function=FunctionCall(name="my_private_plugin_tool", arguments="{}"))
    internal, external = partition_tools([tc_custom])
    assert len(internal) == 0
    assert len(external) == 1
    assert external[0].function.name == "my_private_plugin_tool"
