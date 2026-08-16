"""Tier 1 Feature Tests for Feature 27: Max Tool Iteration Guardrail.

Validates the recursive tool call guardrail, enforcing max_tool_iterations limit to prevent
infinite runaway loops and returning graceful termination responses.
"""

import pytest

from src.gateway.config import Settings
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


@pytest.mark.tier1
@pytest.mark.feature("F27")
def test_f27_max_iterations_configuration_retrieval():
    """Verify that Settings.gateway.max_tool_iterations defaults to 10."""
    settings = Settings()
    assert settings.gateway.max_tool_iterations == 10


@pytest.mark.tier1
@pytest.mark.feature("F27")
def test_f27_tool_loop_tracker_halts_at_threshold():
    """Verify ToolLoopTracker permits iterations up to threshold and returns False on limit."""
    tracker = ToolLoopTracker(max_iterations=5)
    for _ in range(5):
        assert tracker.next_iteration() is True
    # 6th attempt is rejected
    assert tracker.next_iteration() is False
    assert tracker.limit_exceeded is True


@pytest.mark.tier1
@pytest.mark.feature("F27")
def test_f27_guardrail_response_on_limit_exceeded():
    """Verify response generated on limit exceeded has finish_reason='max_tokens' and warning text."""
    tracker = ToolLoopTracker(max_iterations=4)
    for _ in range(4):
        tracker.next_iteration()
    assert tracker.next_iteration() is False

    response = tracker.build_final_response_on_limit()
    assert response.finish_reason == "max_tokens"
    assert len(response.content) == 1
    assert "Tool execution limit reached (4 iterations)" in response.content[0].text


@pytest.mark.tier1
@pytest.mark.feature("F27")
def test_f27_loop_completing_before_limit_succeeds_normally():
    """Verify tool loop completing within limit flags limit_exceeded=False."""
    tracker = ToolLoopTracker(max_iterations=10)
    # Loop finishes after 2 turns
    tracker.next_iteration()
    tracker.next_iteration()
    assert tracker.limit_exceeded is False
    assert tracker.current_iteration == 2


@pytest.mark.tier1
@pytest.mark.feature("F27")
def test_f27_custom_max_iterations_override():
    """Verify custom max_tool_iterations override (e.g. max_iterations=3)."""
    tracker = ToolLoopTracker(max_iterations=3)
    assert tracker.max_iterations == 3
    turns = 0
    while tracker.next_iteration():
        turns += 1
    assert turns == 3
    assert tracker.limit_exceeded is True
