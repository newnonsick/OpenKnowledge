"""Tier 4 Real-World Scenario: Multi-Turn Conversational Coding Agent Session.

Simulates an external developer coding assistant (e.g. Cursor, Continue, Roo Code)
interacting with the real gateway via the OpenAI-compatible /v1/chat/completions
API using model aliases.
"""

import pytest

from tests.e2e.harness.test_env import GatewayMockUpstream, parse_sse_stream


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_f07_f09_multi_turn_coding_session_with_model_alias():
    """Scenario: Multi-turn coding session generating LRU cache -> thread-safe TTL update -> unit tests."""
    async with GatewayMockUpstream() as gw:
        turn1_code = (
            "```python\n"
            "class LRUCache:\n"
            "    def __init__(self, capacity: int):\n"
            "        self.capacity = capacity\n"
            "        self.cache = {}\n"
            "```"
        )
        turn2_code = (
            "```python\n"
            "import threading\n"
            "import time\n\n"
            "class ThreadSafeTTLCache:\n"
            "    def __init__(self, capacity: int, ttl_seconds: float):\n"
            "        self.lock = threading.Lock()\n"
            "        self.capacity = capacity\n"
            "        self.ttl = ttl_seconds\n"
            "        self.cache = {}\n"
            "```"
        )
        turn3_code = (
            "```python\n"
            "import pytest\n\n"
            "def test_cache_expiration():\n"
            "    cache = ThreadSafeTTLCache(capacity=2, ttl_seconds=0.1)\n"
            "    assert cache.capacity == 2\n"
            "```"
        )

        gw.llm.queue_text_response(turn1_code)
        gw.llm.queue_text_response(turn2_code)
        gw.llm.queue_text_response(turn3_code)

        conversation_history = [
            {"role": "system", "content": "You are an expert Python systems engineer."}
        ]

        # Turn 1: Initial request
        conversation_history.append({"role": "user", "content": "Write an LRU Cache in Python."})
        resp1 = await gw.client.post(
            "/v1/chat/completions",
            json={"model": "coding", "messages": conversation_history},
        )
        assert resp1.status_code == 200
        content1 = resp1.json()["choices"][0]["message"]["content"]
        assert "class LRUCache:" in content1
        conversation_history.append({"role": "assistant", "content": content1})

        # Turn 2: Follow-up modification
        conversation_history.append(
            {"role": "user", "content": "Add threading.Lock and TTL expiration support."}
        )
        resp2 = await gw.client.post(
            "/v1/chat/completions",
            json={"model": "coding", "messages": conversation_history},
        )
        assert resp2.status_code == 200
        content2 = resp2.json()["choices"][0]["message"]["content"]
        assert "threading.Lock()" in content2
        conversation_history.append({"role": "assistant", "content": content2})

        # Turn 3: Request unit tests
        conversation_history.append({"role": "user", "content": "Write pytest tests for this cache."})
        resp3 = await gw.client.post(
            "/v1/chat/completions",
            json={"model": "coding", "messages": conversation_history},
        )
        assert resp3.status_code == 200
        content3 = resp3.json()["choices"][0]["message"]["content"]
        assert "test_cache_expiration" in content3

        assert gw.llm.call_count == 3
        # Full conversation history is forwarded upstream each turn:
        # system + (user, assistant) * 2 + final user = 6 messages
        assert len(gw.llm.recorded_requests[2].messages) == 6


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_f07_coding_agent_system_prompt_steering():
    """Scenario: Strict system prompt compliance in developer coding agent."""
    async with GatewayMockUpstream() as gw:
        rust_response = (
            "```rust\n"
            "/// Performs binary search on a sorted slice.\n"
            "pub fn binary_search<T: Ord>(slice: &[T], target: &T) -> Option<usize> {\n"
            "    slice.binary_search(target).ok()\n"
            "}\n"
            "```"
        )
        gw.llm.queue_text_response(rust_response)

        messages = [
            {"role": "system", "content": "You are a Rust compiler specialist. Always write idiomatic Rust."},
            {"role": "user", "content": "Implement binary search."},
        ]
        resp = await gw.client.post(
            "/v1/chat/completions",
            json={"model": "reasoning", "messages": messages},
        )
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "pub fn binary_search" in content
        assert "```rust" in content
        # The system prompt is forwarded upstream as the leading system message
        assert gw.llm.recorded_requests[0].messages[0]["role"] == "system"
        assert "Rust compiler specialist" in gw.llm.recorded_requests[0].messages[0]["content"]


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_f07_f26_coding_agent_streaming_code_generation():
    """Scenario: Streaming code generation with token accumulation and markdown block verification."""
    async with GatewayMockUpstream() as gw:
        chunks = [
            "```python\n",
            "def fibonacci(n: int) -> int:\n",
            "    if n <= 1:\n",
            "        return n\n",
            "    return fibonacci(n - 1) + fibonacci(n - 2)\n",
            "```",
        ]
        gw.llm.queue_stream_response(chunks)

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "coding",
                "messages": [{"role": "user", "content": "Generate recursive fibonacci"}],
                "stream": True,
            },
        )
        assert resp.status_code == 200
        events = await parse_sse_stream(resp)
        tokens = [
            e["choices"][0]["delta"].get("content", "")
            for e in events
            if e.get("choices") and e["choices"] and "content" in e["choices"][0]["delta"]
        ]
        accumulated_code = "".join(tokens)
        assert "def fibonacci(n: int) -> int:" in accumulated_code
        assert accumulated_code.startswith("```python")
        assert accumulated_code.strip().endswith("```")
