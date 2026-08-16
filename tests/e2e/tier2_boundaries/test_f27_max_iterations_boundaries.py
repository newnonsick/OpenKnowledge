"""Tier 2 Boundary Tests for Feature 27: Max Tool Iteration Guardrail.

Tests boundary conditions, max_tool_iterations limits (0, 1, 5), loop termination on threshold, and runaway loop prevention.
"""

from typing import List, Optional
import pytest
from src.gateway.domain.canonical import CanonicalChatResponse, CanonicalTextBlock


class ToolLoopTracker:
    """Tracks recursive tool execution turns and halts at max_iterations limit."""

    def __init__(self, max_iterations: int = 10):
        self.max_iterations = max(1, max_iterations)
        self.current_iteration = 0
        self.limit_exceeded = False

    def next_iteration(self) -> bool:
        """Returns True if next tool execution turn is permitted, False if limit reached."""
        if self.current_iteration >= self.max_iterations:
            self.limit_exceeded = True
            return False
        self.current_iteration += 1
        return True

    def build_final_response_on_limit(self, model: str = "test-model") -> CanonicalChatResponse:
        warning = (
            f"Warning: Tool execution limit reached ({self.max_iterations} iterations). "
            "Terminating loop to prevent runaway recursive calls."
        )
        return CanonicalChatResponse(
            id="chatcmpl-max-iter-reached",
            model=model,
            content=[CanonicalTextBlock(text=warning)],
            finish_reason="max_tokens",
        )


@pytest.mark.tier2
@pytest.mark.feature("F27")
def test_f27_boundary_max_iterations_equals_one():
    """Test boundary: max_iterations=1 permits exactly one tool execution turn and halts on second."""
    tracker = ToolLoopTracker(max_iterations=1)
    assert tracker.next_iteration() is True  # Turn 1 permitted
    assert tracker.next_iteration() is False  # Turn 2 blocked
    assert tracker.limit_exceeded is True


@pytest.mark.tier2
@pytest.mark.feature("F27")
def test_f27_boundary_max_iterations_zero_or_negative_defaults_to_one():
    """Test boundary: max_iterations <= 0 safely clamps to at least 1 iteration."""
    tracker_zero = ToolLoopTracker(max_iterations=0)
    assert tracker_zero.max_iterations == 1

    tracker_neg = ToolLoopTracker(max_iterations=-5)
    assert tracker_neg.max_iterations == 1


@pytest.mark.tier2
@pytest.mark.feature("F27")
def test_f27_boundary_exact_threshold_termination():
    """Test boundary: with max_iterations=5, loop executes exactly 5 times and terminates cleanly."""
    tracker = ToolLoopTracker(max_iterations=5)
    count = 0
    while tracker.next_iteration():
        count += 1
    assert count == 5
    assert tracker.current_iteration == 5
    assert tracker.limit_exceeded is True


@pytest.mark.tier2
@pytest.mark.feature("F27")
def test_f27_boundary_guardrail_response_content_and_finish_reason():
    """Test boundary: when limit is exceeded, produced response includes descriptive warning."""
    tracker = ToolLoopTracker(max_iterations=3)
    for _ in range(3):
        tracker.next_iteration()
    assert tracker.next_iteration() is False

    resp = tracker.build_final_response_on_limit()
    assert resp.finish_reason == "max_tokens"
    assert len(resp.content) == 1
    assert isinstance(resp.content[0], CanonicalTextBlock)
    assert "Tool execution limit reached" in resp.content[0].text
    assert "3 iterations" in resp.content[0].text


@pytest.mark.tier2
@pytest.mark.feature("F27")
def test_f27_boundary_unreached_limit_does_not_trigger_warning():
    """Test boundary: loop completing within limits does not flag limit_exceeded."""
    tracker = ToolLoopTracker(max_iterations=10)
    tracker.next_iteration()
    tracker.next_iteration()
    assert tracker.limit_exceeded is False
    assert tracker.current_iteration == 2
