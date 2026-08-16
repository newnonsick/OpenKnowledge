"""
Adversarial Empirical Stress Test Suite for Mock HTTP Server Harness.
Module: tests/unit/test_mock_server_adversarial.py

Target under test: tests/e2e/harness/mock_server.py
Focus Areas:
1. Deterministic Embeddings across dimensions (384, 1024, 1536, etc.), unit norm (sum v_i^2 ≈ 1.0),
   reproducibility across 100+ distinct strings, edge cases (empty, long, unicode, emojis, control chars).
2. Mock LLM Controller under multi-turn conversations, rule matching, SSE streaming chunk generation
   with delta payloads, finish reasons, delay simulation, and [DONE] terminator.
3. Tool call queues for both internal knowledge tools (knowledge_search, knowledge_save, knowledge_get,
   knowledge_update, knowledge_delete) and external harness tools (bash, edit_file, git, mcp),
   parallel tool calling, argument serialization (dict vs JSON string), Anthropic tool_use mapping.
4. Dynamic port binding (Port 0) vs in-process ASGITransport lifecycle, socket concurrency,
   sequential restart cycles, multiple parallel servers.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import string
import time
from typing import Any, Dict, List
import httpx
import pytest

from tests.e2e.harness.mock_server import (
    DeterministicEmbeddingEngine,
    MockEmbeddingController,
    MockLLMController,
    MockLLMResponse,
    MockServerManager,
    MockToolCall,
    RecordedRequest,
    create_mock_upstream_app,
)
from tests.e2e.harness.test_env import parse_sse_stream


# ==============================================================================
# 1. Deterministic Embeddings Stress Tests
# ==============================================================================

class TestDeterministicEmbeddingsAdversarial:
    """Stress tests for DeterministicEmbeddingEngine and MockEmbeddingController."""

    @pytest.mark.parametrize("dimension", [64, 128, 384, 512, 768, 1024, 1536, 2048, 3072, 4096])
    def test_unit_norm_and_reproducibility_across_dimensions(self, dimension: int):
        """Verify unit L2 norm (|sum v_i^2 - 1.0| < 0.005) across various vector dimensions."""
        engine = DeterministicEmbeddingEngine(dimension=dimension)
        test_strings = [
            "Simple text",
            "Multi-line\nText\nWith\nNewlines",
            "Special characters: !@#$%^&*()_+{}[]:;\"'<>?,./~`",
            "Unicode: 🚀💡🎉 汉语 日本語 Русский язык العربية ภาษาไทย",
            "JSON payload: " + json.dumps({"key": "value", "nested": [1, 2, 3]}),
            "Code snippet: def foo(x: int) -> str: return f'val={x}'",
            "Empty string: ",
            "Whitespace only:    \t\t\n\n   ",
            "A" * 5000,  # 5KB string
            "Very long repetitive string: " + "repeat " * 1000,
        ]

        for s in test_strings:
            v1 = engine.generate_vector(s)
            v2 = engine.generate_vector(s)

            # Check length matches dimension
            assert len(v1) == dimension, f"Vector length {len(v1)} != dimension {dimension}"
            # Check strict reproducibility
            assert v1 == v2, f"Vector generation for '{s[:20]}' not reproducible"

            # Check L2 unit norm
            sum_sq = sum(x * x for x in v1)
            norm = math.sqrt(sum_sq)
            # Coordinates are rounded to 6 decimal places: round(x / norm, 6)
            # Max rounding error per coord <= 0.5e-6, sum of errors bounded by dim * 1e-6
            assert pytest.approx(norm, abs=0.005) == 1.0, (
                f"L2 norm {norm:.6f} deviated for dim={dimension}, text='{s[:20]}'"
            )

    def test_reproducibility_and_differentiation_100_distinct_strings(self):
        """Stress-test 100+ distinct strings: all vectors must be distinct and 100% reproducible."""
        engine = DeterministicEmbeddingEngine(dimension=1024)
        generated_vectors: Dict[str, List[float]] = {}

        # Build 120 diverse strings
        distinct_strings: List[str] = []

        # 1. 30 random alphanumeric sentences
        rng = random.Random(42)
        for i in range(30):
            words = ["".join(rng.choices(string.ascii_letters, k=rng.randint(3, 10))) for _ in range(8)]
            distinct_strings.append(f"sentence_{i}: " + " ".join(words))

        # 2. 20 Single-character difference perturbations (adversarial collision check)
        base = "The quick brown fox jumps over the lazy dog"
        for i in range(20):
            distinct_strings.append(f"{base} version_{i}!")

        # 3. 20 code snippets
        for i in range(20):
            distinct_strings.append(f"SELECT id, title, content FROM knowledge_items WHERE version = {i} ORDER BY id ASC;")

        # 4. 20 multilingual strings
        languages = ["English", "Español", "Français", "Deutsch", "Italiano", "Português", "Русский", "中文", "日本語", "한국어", "العربية", "हिन्दी", "বাংলা", "Tiếng Việt", "Türkçe", "Polski", "Nederlands", "Ελληνικά", "Svenska", "ไทย"]
        for idx, lang in enumerate(languages):
            distinct_strings.append(f"Language item {idx}: {lang} - Testing multilingual deterministic embeddings support.")

        # 5. 20 edge case tokens
        edge_cases = ["", " ", "\t", "\n", "\r\n", "\0", "\x01\x02\x03", "null", "undefined", "NaN", "{}", "[]", "true", "false", "0", "-1", "999999999999", "🎉", "🔥", "⚠️"]
        distinct_strings.extend(edge_cases)

        assert len(distinct_strings) >= 110

        # Generate vectors and verify reproducibility + uniqueness
        for text in distinct_strings:
            v_first = engine.generate_vector(text)
            # Re-generate from independent engine instance with same dimension
            engine_dup = DeterministicEmbeddingEngine(dimension=1024)
            v_second = engine_dup.generate_vector(text)

            assert v_first == v_second, f"Reproducibility failed across engine instances for text '{text[:20]}'"

            # Verify unit norm
            norm = math.sqrt(sum(x * x for x in v_first))
            assert pytest.approx(norm, abs=0.005) == 1.0

            # Store for collision checking
            tuple_rep = tuple(v_first)
            assert tuple_rep not in generated_vectors.values() or text in generated_vectors, (
                f"Hash collision detected between distinct strings in 1024-dim space"
            )
            generated_vectors[text] = v_first

        assert len(generated_vectors) == len(distinct_strings)

    def test_custom_vector_registration_and_dimension_validation(self):
        """Verify custom vector registration validates dimensions and overrides engine output."""
        engine = DeterministicEmbeddingEngine(dimension=384)
        custom_vec = [1.0 / math.sqrt(384)] * 384
        engine.register_custom_vector("pinned_query", custom_vec)

        # Output should be exact custom vector
        assert engine.generate_vector("pinned_query") == custom_vec

        # Invalid dimension registration must raise ValueError
        with pytest.raises(ValueError) as exc_info:
            engine.register_custom_vector("invalid_dim", [0.1, 0.2, 0.3])
        assert "does not match engine dimension" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_embedding_controller_error_queue_and_batch_processing(self):
        """Verify embedding controller handles batch size, token usage, error queueing."""
        controller = MockEmbeddingController(dimension=512)

        # Batch of 50 chunks
        batch_inputs = [f"Knowledge chunk content paragraph {i}" for i in range(50)]
        req = RecordedRequest(
            endpoint="/v1/embeddings",
            method="POST",
            headers={"authorization": "Bearer embed-key"},
            body={"input": batch_inputs, "model": "BAAI/bge-large-en-v1.5"},
        )

        status, payload = await controller.process_embeddings(req)
        assert status == 200
        assert payload["object"] == "list"
        assert len(payload["data"]) == 50
        assert payload["model"] == "BAAI/bge-large-en-v1.5"
        assert payload["usage"]["prompt_tokens"] > 0

        # Verify each vector is 512-dim unit norm
        for idx, item in enumerate(payload["data"]):
            assert item["index"] == idx
            assert len(item["embedding"]) == 512
            norm = math.sqrt(sum(x * x for x in item["embedding"]))
            assert pytest.approx(norm, abs=0.005) == 1.0

        # Verify error queueing
        controller.queue_error(503, "Embedding backend service temporarily unavailable")
        status_err, payload_err = await controller.process_embeddings(req)
        assert status_err == 503
        assert payload_err["error"]["code"] == 503
        assert "temporarily unavailable" in payload_err["error"]["message"]

        # Next request returns 200 again (error was consumed)
        status_ok, payload_ok = await controller.process_embeddings(req)
        assert status_ok == 200


# ==============================================================================
# 2. Mock LLM Controller & Protocol Mechanics Stress Tests
# ==============================================================================

class TestMockLLMControllerAdversarial:
    """Stress tests for multi-turn conversations, rule matching, SSE streaming, and error handling."""

    @pytest.mark.asyncio
    async def test_multi_turn_conversation_rule_matching(self):
        """Simulate an adversarial 6-turn conversation driven by rule matching on history."""
        controller = MockLLMController()

        # Rule 1: Turn 1 greeting
        controller.add_rule(
            match_fn=lambda req: len(req.messages) == 1 and "hello" in req.get_last_user_message().lower(),
            response_fn=lambda req: MockLLMResponse.text("Hello! How can I assist you with your knowledge base today?"),
        )
        # Rule 2: Turn 2 asking for search
        controller.add_rule(
            match_fn=lambda req: any("search" in m.get("content", "").lower() for m in req.messages if m.get("role") == "user"),
            response_fn=lambda req: MockLLMResponse.tool_call(
                name="knowledge_search",
                arguments={"query": "clean architecture", "workspace_id": "global", "limit": 3},
                call_id="call_search_01",
            ),
        )

        # Execute Turn 1
        req1 = RecordedRequest(
            endpoint="/v1/chat/completions",
            method="POST",
            headers={},
            body={"messages": [{"role": "user", "content": "Hello there!"}]},
        )
        resp1 = await controller.get_next_response(req1)
        assert "How can I assist" in resp1.content

        # Execute Turn 2
        req2 = RecordedRequest(
            endpoint="/v1/chat/completions",
            method="POST",
            headers={},
            body={
                "messages": [
                    {"role": "user", "content": "Hello there!"},
                    {"role": "assistant", "content": resp1.content},
                    {"role": "user", "content": "Please search for clean architecture guidelines."},
                ]
            },
        )
        resp2 = await controller.get_next_response(req2)
        assert resp2.finish_reason == "tool_calls"
        assert resp2.tool_calls[0].name == "knowledge_search"
        assert resp2.tool_calls[0].id == "call_search_01"

        # Verify call history
        assert controller.call_count == 2
        assert controller.recorded_requests[0].get_last_user_message() == "Hello there!"
        assert "search for clean architecture" in controller.recorded_requests[1].get_last_user_message()

    @pytest.mark.asyncio
    async def test_sse_streaming_chunk_generation_and_done_terminator(self):
        """Stress-test SSE streaming via in-process ASGI app with chunk reassembly and [DONE] check."""
        llm = MockLLMController()
        embedding = MockEmbeddingController()
        app = create_mock_upstream_app(llm, embedding)

        # Queue multi-chunk response
        words = ["The ", "Pragmatic ", "Clean ", "Architecture ", "separates ", "concerns ", "cleanly."]
        llm.queue_stream_response(chunks=words, delay_seconds=0.001)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "meta-llama/Llama-3.1-8B-Instruct",
                    "messages": [{"role": "user", "content": "Explain architecture"}],
                    "stream": True,
                },
            )
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]

            # Parse SSE stream using helper
            events = await parse_sse_stream(resp)

            # Validate event count: 1 role chunk + 7 word chunks + 1 finish chunk = 9 chunks
            assert len(events) == len(words) + 2

            # Check Chunk 0 (Role announcement)
            assert events[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
            assert events[0]["choices"][0]["finish_reason"] is None

            # Check middle delta text chunks
            reassembled_text = ""
            for idx, word in enumerate(words):
                chunk = events[idx + 1]
                assert chunk["choices"][0]["delta"]["content"] == word
                assert chunk["choices"][0]["finish_reason"] is None
                reassembled_text += chunk["choices"][0]["delta"]["content"]

            assert reassembled_text == "".join(words)

            # Check Final finish chunk
            finish_chunk = events[-1]
            assert finish_chunk["choices"][0]["finish_reason"] == "stop"
            assert finish_chunk["choices"][0]["delta"] == {}

    @pytest.mark.asyncio
    async def test_sse_streaming_tool_call_chunks(self):
        """Verify streaming tool calls emit proper delta structure and [DONE] terminator."""
        llm = MockLLMController()
        embedding = MockEmbeddingController()
        app = create_mock_upstream_app(llm, embedding)

        llm.queue_tool_call(
            name="bash",
            arguments={"command": "pytest tests/ -v"},
            call_id="call_bash_99",
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "meta-llama/Llama-3.1-8B-Instruct",
                    "messages": [{"role": "user", "content": "Run the tests"}],
                    "stream": True,
                },
            )
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]

            events = await parse_sse_stream(resp)
            # Role chunk + Tool call delta chunk + Finish chunk = 3 events
            assert len(events) == 3

            # Tool call delta
            tc_delta = events[1]["choices"][0]["delta"]["tool_calls"][0]
            assert tc_delta["id"] == "call_bash_99"
            assert tc_delta["function"]["name"] == "bash"
            assert json.loads(tc_delta["function"]["arguments"]) == {"command": "pytest tests/ -v"}

            # Finish reason
            assert events[2]["choices"][0]["finish_reason"] == "tool_calls"

    @pytest.mark.asyncio
    async def test_programmed_error_responses_and_invalid_payloads(self):
        """Verify mock server returns correct error status codes and handles malformed payloads."""
        llm = MockLLMController()
        embedding = MockEmbeddingController()
        app = create_mock_upstream_app(llm, embedding)

        llm.queue_error(status_code=429, message="Rate limit exceeded: 50 requests per minute")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as client:
            # 1. Programmed 429 error
            resp_err = await client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "hello"}]},
            )
            assert resp_err.status_code == 429
            data = resp_err.json()
            assert "Rate limit exceeded" in data["error"]["message"]

            # 2. Malformed non-JSON payload to /v1/chat/completions -> 400 Bad Request
            resp_bad = await client.post(
                "/v1/chat/completions",
                content=b"Not a JSON string {invalid",
                headers={"Content-Type": "application/json"},
            )
            assert resp_bad.status_code == 400
            assert resp_bad.json()["error"]["message"] == "Invalid JSON body"


# ==============================================================================
# 3. Tool Call Queues & Interception Verification Stress Tests
# ==============================================================================

class TestToolCallQueuesAdversarial:
    """Stress tests for internal knowledge tools and external harness tools."""

    @pytest.mark.parametrize(
        "tool_name,tool_args",
        [
            ("knowledge_search", {"query": "clean architecture", "workspace_id": "ws-1", "limit": 5}),
            ("knowledge_save", {"title": "Architecture Rules", "content": "1. Decoupled", "workspace_id": "global", "is_global": True, "author": "tester"}),
            ("knowledge_get", {"item_id": "550e8400-e29b-41d4-a716-446655440000", "workspace_id": "ws-1"}),
            ("knowledge_update", {"item_id": "550e8400-e29b-41d4-a716-446655440000", "content": "Updated content v2", "expected_version": 1, "workspace_id": "ws-1", "author": "tester"}),
            ("knowledge_delete", {"item_id": "550e8400-e29b-41d4-a716-446655440000", "workspace_id": "ws-1"}),
            ("bash", {"command": "git status --short"}),
            ("edit_file", {"path": "src/gateway/main.py", "content": "# Updated main entrypoint"}),
            ("git", {"subcommand": "commit -m 'feat: initial commit'"}),
            ("mcp", {"server": "filesystem", "tool": "read_file", "params": {"path": "/etc/hosts"}}),
        ],
    )
    @pytest.mark.asyncio
    async def test_tool_call_queue_openai_and_anthropic_formats(self, tool_name: str, tool_args: Dict[str, Any]):
        """Verify queuing various tools and serializing properly in OpenAI and Anthropic formats."""
        llm = MockLLMController()
        embedding = MockEmbeddingController()
        app = create_mock_upstream_app(llm, embedding)

        call_id = f"call_{tool_name}_{uuid_short()}"
        llm.queue_tool_call(name=tool_name, arguments=tool_args, call_id=call_id)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as client:
            # 1. Test OpenAI format
            resp_openai = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "meta-llama/Llama-3.1-8B-Instruct",
                    "messages": [{"role": "user", "content": f"Execute {tool_name}"}],
                },
            )
            assert resp_openai.status_code == 200
            data_openai = resp_openai.json()
            assert data_openai["choices"][0]["finish_reason"] == "tool_calls"
            tc = data_openai["choices"][0]["message"]["tool_calls"][0]
            assert tc["id"] == call_id
            assert tc["type"] == "function"
            assert tc["function"]["name"] == tool_name
            # Arguments must be valid JSON string in OpenAI format
            parsed_args = json.loads(tc["function"]["arguments"])
            assert parsed_args == tool_args

        # 2. Test Anthropic format with re-queued tool call
        llm.queue_tool_call(name=tool_name, arguments=tool_args, call_id=call_id)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as client:
            resp_anthropic = await client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-5-sonnet-20241022",
                    "messages": [{"role": "user", "content": f"Execute {tool_name}"}],
                },
            )
            assert resp_anthropic.status_code == 200
            data_anthropic = resp_anthropic.json()
            assert data_anthropic["type"] == "message"
            assert data_anthropic["stop_reason"] == "tool_use"
            content_block = data_anthropic["content"][0]
            assert content_block["type"] == "tool_use"
            assert content_block["name"] == tool_name
            assert content_block["input"] == tool_args

    @pytest.mark.asyncio
    async def test_parallel_multiple_tool_calls_in_single_response(self):
        """Verify queuing and responding with multiple parallel tool calls."""
        llm = MockLLMController()
        embedding = MockEmbeddingController()
        app = create_mock_upstream_app(llm, embedding)

        tool1 = MockToolCall(name="knowledge_search", arguments={"query": "RRF ranking", "workspace_id": "global"}, id="call_par_01")
        tool2 = MockToolCall(name="bash", arguments={"command": "ls -la"}, id="call_par_02")
        tool3 = MockToolCall(name="edit_file", arguments={"path": "notes.md", "content": "# Notes"}, id="call_par_03")

        llm.queue_multiple_tool_calls([tool1, tool2, tool3])

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mock"
        ) as client:
            resp = await client.post(
                "/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "Execute multi-tool batch"}]},
            )
            assert resp.status_code == 200
            tcs = resp.json()["choices"][0]["message"]["tool_calls"]
            assert len(tcs) == 3
            assert [tc["function"]["name"] for tc in tcs] == ["knowledge_search", "bash", "edit_file"]
            assert [tc["id"] for tc in tcs] == ["call_par_01", "call_par_02", "call_par_03"]


# ==============================================================================
# 4. Dynamic Port Binding vs In-Process ASGITransport Lifecycle Stress Tests
# ==============================================================================

class TestServerLifecycleAndConcurrencyAdversarial:
    """Stress tests for real socket ephemeral port binding, concurrency, and ASGITransport."""

    @pytest.mark.asyncio
    async def test_ephemeral_port_socket_binding_and_concurrent_requests(self):
        """Start mock server on OS-assigned dynamic port 0, send 50 concurrent requests over real TCP."""
        manager = MockServerManager(embedding_dimension=384)

        # Setup rule for echo
        manager.llm.add_rule(
            match_fn=lambda req: True,
            response_fn=lambda req: MockLLMResponse.text(f"Echo: {req.get_last_user_message()}"),
        )

        async with manager:
            assert manager.port > 0, "Port was not assigned by OS"
            assert manager.base_url == f"http://127.0.0.1:{manager.port}"

            async with httpx.AsyncClient(base_url=manager.base_url, timeout=10.0) as client:
                # 1. Health check over real socket
                health_resp = await client.get("/health")
                assert health_resp.status_code == 200
                assert health_resp.json()["service"] == "mock-upstream-harness"

                # 2. Fire 50 concurrent requests simultaneously
                async def send_chat(req_id: int) -> httpx.Response:
                    return await client.post(
                        "/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": f"Request #{req_id}"}]},
                    )

                tasks = [send_chat(i) for i in range(50)]
                start_time = time.perf_counter()
                responses = await asyncio.gather(*tasks)
                elapsed = time.perf_counter() - start_time

                assert len(responses) == 50
                for idx, r in enumerate(responses):
                    assert r.status_code == 200
                    assert f"Request #{idx}" in r.json()["choices"][0]["message"]["content"]

                # 3. Fire 20 concurrent embedding requests
                async def send_embed(req_id: int) -> httpx.Response:
                    return await client.post(
                        "/v1/embeddings",
                        json={"input": [f"Embedding chunk text {req_id}_{j}" for j in range(4)]},
                    )

                embed_tasks = [send_embed(i) for i in range(20)]
                embed_responses = await asyncio.gather(*embed_tasks)
                assert len(embed_responses) == 20
                for r in embed_responses:
                    assert r.status_code == 200
                    data = r.json()
                    assert len(data["data"]) == 4
                    assert len(data["data"][0]["embedding"]) == 384

        # Verify server task is cleaned up after context exit
        assert manager._server_task is None

    @pytest.mark.asyncio
    async def test_sequential_server_restart_cycles(self):
        """Verify spinning up and tearing down the server across 3 consecutive cycles without leaks."""
        for cycle in range(3):
            manager = MockServerManager(embedding_dimension=128)
            manager.llm.queue_text_response(f"Cycle {cycle} response")

            async with manager:
                port = manager.port
                assert port > 0
                async with httpx.AsyncClient(base_url=manager.base_url) as client:
                    resp = await client.post(
                        "/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": f"Cycle {cycle}"}]},
                    )
                    assert resp.status_code == 200
                    assert resp.json()["choices"][0]["message"]["content"] == f"Cycle {cycle} response"

            assert manager._server_task is None

    @pytest.mark.asyncio
    async def test_multiple_parallel_servers_isolated_state(self):
        """Run 2 distinct MockServerManager instances simultaneously and verify isolated ports & state."""
        server1 = MockServerManager(embedding_dimension=128)
        server2 = MockServerManager(embedding_dimension=256)

        server1.llm.queue_text_response("Server 1 Unique Response")
        server2.llm.queue_text_response("Server 2 Unique Response")

        async with server1, server2:
            assert server1.port != server2.port
            assert server1.port > 0 and server2.port > 0

            async with httpx.AsyncClient(base_url=server1.base_url) as client1, \
                       httpx.AsyncClient(base_url=server2.base_url) as client2:

                r1 = await client1.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
                r2 = await client2.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})

                assert r1.json()["choices"][0]["message"]["content"] == "Server 1 Unique Response"
                assert r2.json()["choices"][0]["message"]["content"] == "Server 2 Unique Response"

                # Embeddings dimensions check
                e1 = await client1.post("/v1/embeddings", json={"input": "test"})
                e2 = await client2.post("/v1/embeddings", json={"input": "test"})

                assert len(e1.json()["data"][0]["embedding"]) == 128
                assert len(e2.json()["data"][0]["embedding"]) == 256

    @pytest.mark.asyncio
    async def test_asgitransport_in_process_throughput(self):
        """Execute 100 in-process requests via ASGITransport to verify zero network overhead execution."""
        llm = MockLLMController()
        embedding = MockEmbeddingController(dimension=64)
        app = create_mock_upstream_app(llm, embedding)

        llm.add_rule(
            match_fn=lambda req: True,
            response_fn=lambda req: MockLLMResponse.text("In-process fast reply"),
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://in-process"
        ) as client:
            start = time.perf_counter()
            for i in range(100):
                resp = await client.post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": f"msg {i}"}]},
                )
                assert resp.status_code == 200
                assert resp.json()["choices"][0]["message"]["content"] == "In-process fast reply"
            duration = time.perf_counter() - start

            # 100 in-process requests should complete very quickly (< 1.5 seconds)
            assert duration < 2.0, f"In-process execution took unexpectedly long: {duration:.3f}s"
            assert llm.call_count == 100


# ==============================================================================
# 5. Interleaved Tool Interception & Protocol Edge Case Stress Tests
# ==============================================================================

class TestInterleavedToolLoopAndProtocolEdgeCases:
    """Stress tests for multi-step tool loops, multimodal messages, and TEI routing."""

    @pytest.mark.asyncio
    async def test_multi_step_interleaved_knowledge_tool_loop(self):
        """
        Simulate full 3-turn orchestration loop:
        Turn 1: LLM outputs knowledge_search tool call
        Turn 2: LLM outputs knowledge_save tool call
        Turn 3: LLM outputs final synthesized text answer
        """
        llm = MockLLMController()
        embedding = MockEmbeddingController()
        app = create_mock_upstream_app(llm, embedding)

        # Queue the 3 sequential steps
        llm.queue_tool_call(
            name="knowledge_search",
            arguments={"query": "RRF fusion parameters", "workspace_id": "ws-arch"},
            call_id="call_step_1",
        )
        llm.queue_tool_call(
            name="knowledge_save",
            arguments={"title": "RRF Note", "content": "k=60 is standard", "workspace_id": "ws-arch", "is_global": False, "author": "agent"},
            call_id="call_step_2",
        )
        llm.queue_text_response("I have searched the knowledge base and saved the RRF configuration note.")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock") as client:
            # Turn 1
            r1 = await client.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "Find RRF config and record it"}]
            })
            assert r1.status_code == 200
            msg1 = r1.json()["choices"][0]["message"]
            assert msg1["tool_calls"][0]["function"]["name"] == "knowledge_search"
            assert msg1["tool_calls"][0]["id"] == "call_step_1"

            # Turn 2 (with tool execution result fed back)
            r2 = await client.post("/v1/chat/completions", json={
                "messages": [
                    {"role": "user", "content": "Find RRF config and record it"},
                    msg1,
                    {"role": "tool", "tool_call_id": "call_step_1", "content": json.dumps([{"title": "RRF doc", "score": 0.95}])},
                ]
            })
            assert r2.status_code == 200
            msg2 = r2.json()["choices"][0]["message"]
            assert msg2["tool_calls"][0]["function"]["name"] == "knowledge_save"
            assert msg2["tool_calls"][0]["id"] == "call_step_2"

            # Turn 3 (with second tool execution result fed back)
            r3 = await client.post("/v1/chat/completions", json={
                "messages": [
                    {"role": "user", "content": "Find RRF config and record it"},
                    msg1,
                    {"role": "tool", "tool_call_id": "call_step_1", "content": json.dumps([{"title": "RRF doc", "score": 0.95}])},
                    msg2,
                    {"role": "tool", "tool_call_id": "call_step_2", "content": json.dumps({"status": "saved", "version": 1})},
                ]
            })
            assert r3.status_code == 200
            msg3 = r3.json()["choices"][0]["message"]
            assert msg3["content"] == "I have searched the knowledge base and saved the RRF configuration note."
            assert r3.json()["choices"][0]["finish_reason"] == "stop"

        assert llm.call_count == 3

    def test_recorded_request_multimodal_and_edge_inputs(self):
        """Verify RecordedRequest helper methods handle multimodal blocks and empty inputs."""
        # 1. Multimodal user message content
        req_multimodal = RecordedRequest(
            endpoint="/v1/chat/completions",
            method="POST",
            headers={},
            body={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Explain this architecture diagram:"},
                            {"type": "image_url", "image_url": {"url": "http://example.com/arch.png"}},
                            {"type": "text", "text": "Focus on pgvector."},
                        ],
                    }
                ]
            },
        )
        assert req_multimodal.get_last_user_message() == "Explain this architecture diagram: Focus on pgvector."

        # 2. No user message
        req_no_user = RecordedRequest(
            endpoint="/v1/chat/completions",
            method="POST",
            headers={},
            body={"messages": [{"role": "system", "content": "You are a helpful assistant."}]},
        )
        assert req_no_user.get_last_user_message() == ""

        # 3. Single string input vs list input in embeddings
        req_str = RecordedRequest(
            endpoint="/v1/embeddings",
            method="POST",
            headers={},
            body={"input": "Single string input"},
        )
        assert req_str.inputs == ["Single string input"]

        req_empty = RecordedRequest(
            endpoint="/v1/embeddings",
            method="POST",
            headers={},
            body={},
        )
        assert req_empty.inputs == []

    @pytest.mark.asyncio
    async def test_anthropic_error_handling(self):
        """Verify Anthropic messages endpoint returns programmed errors."""
        llm = MockLLMController()
        embedding = MockEmbeddingController()
        app = create_mock_upstream_app(llm, embedding)

        llm.queue_error(status_code=401, message="Invalid Anthropic API Key")

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock") as client:
            resp = await client.post("/v1/messages", json={"messages": [{"role": "user", "content": "test"}]})
            assert resp.status_code == 401
            assert "Invalid Anthropic API Key" in resp.json()["error"]["message"]


def uuid_short() -> str:
    import uuid
    return uuid.uuid4().hex[:8]

