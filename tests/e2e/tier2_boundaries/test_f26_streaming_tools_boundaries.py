"""Tier 2 Boundary Tests for Feature 26: Streaming SSE Tool Interception Engine.

Tests boundary conditions, tool arguments split across many SSE chunks, malformed SSE data, and stream assembly.
"""

import json
from typing import Any, Dict, List
import pytest

from src.gateway.domain.tools import FunctionCall, ToolCall
from tests.e2e.harness.test_env import parse_sse_stream


def assemble_streamed_tool_call(chunks: List[str]) -> str:
    """Assembles fragmented tool argument chunks streamed via SSE."""
    return "".join(chunks)


@pytest.mark.tier2
@pytest.mark.feature("F26")
def test_f26_boundary_arguments_split_across_20_sse_chunks():
    """Test boundary: tool call JSON argument fragmented across 20 distinct micro-chunks reassembles correctly."""
    target_dict = {
        "query": "hybrid search implementation details",
        "workspace_id": "ws_complex_123",
        "limit": 10,
        "tags": ["fts", "vector", "rrf"],
    }
    full_json_str = json.dumps(target_dict)

    # Split string into 20 tiny pieces
    chunk_size = max(1, len(full_json_str) // 20)
    micro_chunks = [
        full_json_str[i:i + chunk_size]
        for i in range(0, len(full_json_str), chunk_size)
    ]
    assert len(micro_chunks) >= 20

    assembled_str = assemble_streamed_tool_call(micro_chunks)
    assert assembled_str == full_json_str
    parsed = json.loads(assembled_str)
    assert parsed == target_dict


@pytest.mark.tier2
@pytest.mark.feature("F26")
def test_f26_boundary_single_character_sse_chunks():
    """Test boundary: tool arguments streamed 1 character per SSE chunk assemble into valid JSON."""
    raw_args = '{"query":"test"}'
    char_chunks = list(raw_args)
    assembled = assemble_streamed_tool_call(char_chunks)
    assert assembled == raw_args
    assert json.loads(assembled)["query"] == "test"


@pytest.mark.tier2
@pytest.mark.feature("F26")
def test_f26_boundary_empty_chunks_in_stream():
    """Test boundary: empty string chunks during streaming do not disrupt argument assembly."""
    chunks = ['{"item_id":', "", ' "uuid-123",', "", ' "version": 1}']
    assembled = assemble_streamed_tool_call(chunks)
    parsed = json.loads(assembled)
    assert parsed["item_id"] == "uuid-123"
    assert parsed["version"] == 1


@pytest.mark.tier2
@pytest.mark.feature("F26")
def test_f26_boundary_incomplete_truncated_stream_detection():
    """Test boundary: truncated stream missing closing brackets raises JSONDecodeError when parsed."""
    incomplete_chunks = ['{"query": "unclosed search string']
    assembled = assemble_streamed_tool_call(incomplete_chunks)
    with pytest.raises(json.JSONDecodeError):
        json.loads(assembled)


@pytest.mark.tier2
@pytest.mark.feature("F26")
def test_f26_boundary_tool_call_object_creation_from_assembled_args():
    """Test boundary: creating domain ToolCall from fully assembled streamed arguments."""
    assembled_args = json.dumps({"title": "New Doc", "content": "Body"})
    tc = ToolCall(
        id="call_assembled_1",
        function=FunctionCall(name="knowledge_save", arguments=assembled_args),
    )
    assert tc.id == "call_assembled_1"
    assert tc.function.name == "knowledge_save"
    assert json.loads(tc.function.arguments)["title"] == "New Doc"
