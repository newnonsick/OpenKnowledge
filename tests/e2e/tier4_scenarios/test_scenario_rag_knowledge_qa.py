"""Tier 4 Real-World Scenario: RAG Knowledge Q&A Lifecycle.

Simulates an end-to-end developer question-answering workflow through the REAL
gateway stack: file upload -> parsing -> chunking -> mock embedding -> pgvector
indexing -> knowledge_search interception (hybrid FTS + vector + RRF) -> LLM
synthesis with citations.
"""

import httpx
import pytest

from tests.e2e.harness.test_env import GatewayMockUpstream


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_f16_f20_f21_f22_f23_rag_knowledge_qa_complete_lifecycle():
    """Scenario: upload an architecture doc, ask a question, gateway intercepts
    knowledge_search, retrieves the ingested chunks, and synthesizes an answer."""
    from tests.e2e.harness.mock_server import MockLLMResponse

    async with GatewayMockUpstream(embedding_dimension=384) as gw:
        doc_text = (
            "# Gateway Authentication Architecture\n\n"
            "## Token Validation Flow\n"
            "The gateway verifies Bearer tokens and x-api-key headers using constant-time comparison. "
            "All configured keys in GATEWAY_API_KEYS are granted full access across all workspace scopes.\n\n"
            "## OCC Version Control\n"
            "Knowledge updates enforce Optimistic Concurrency Control by comparing expected_version "
            "against the latest persisted revision version in the database."
        )

        # 1. Upload the document through the real /v1/files/upload pipeline
        upload_resp = await gw.client.post(
            "/v1/files/upload",
            files={"file": ("auth_architecture.md", doc_text.encode("utf-8"), "text/markdown")},
            data={"workspace_id": "global"},
        )
        assert upload_resp.status_code == 201, upload_resp.text
        upload_data = upload_resp.json()
        assert upload_data["filename"] == "auth_architecture.md"
        assert upload_data["total_chunks"] >= 1

        # 2. Ask a question; the model responds with an internal knowledge_search call
        gw.llm.queue_response(
            MockLLMResponse.tool_call(
                name="knowledge_search",
                arguments={"query": "How does the gateway validate API keys and tokens?"},
                call_id="call_rag_search_1",
            )
        )
        # 3. After receiving retrieval results, the model answers with citations
        gw.llm.queue_text_response(
            "Based on [auth_architecture.md], the gateway verifies Bearer tokens and "
            "x-api-key headers using constant-time comparison against GATEWAY_API_KEYS."
        )

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "How does the gateway validate API keys and tokens?"}],
            },
        )
        assert resp.status_code == 200
        answer_text = resp.json()["choices"][0]["message"]["content"]

        # The interception loop ran: search turn + synthesis turn
        assert gw.llm.call_count == 2
        # The second upstream turn received the executed knowledge_search result
        # containing the ingested document content
        second_turn_messages = gw.llm.recorded_requests[1].messages
        tool_msgs = [m for m in second_turn_messages if m.get("role") == "tool"]
        assert tool_msgs, "knowledge_search result must be fed back upstream"
        assert "constant-time comparison" in tool_msgs[0]["content"]

        # The client only sees the final synthesized answer with citations
        assert "[auth_architecture.md]" in answer_text
        assert "constant-time comparison" in answer_text


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_f23_rag_knowledge_qa_multi_document_blended_context():
    """Scenario: two ingested documents blend into one retrieval result set and
    the synthesized answer cites both sources."""
    from tests.e2e.harness.mock_server import MockLLMResponse

    async with GatewayMockUpstream(embedding_dimension=384) as gw:
        doc1 = (
            "API Specification: POST /v1/chat/completions supports streaming SSE "
            "responses for real-time token delivery."
        )
        doc2 = (
            "Security Policy: Passwords and API keys must never be logged in plain text. "
            "Rotate gateway keys every 90 days."
        )

        up1 = await gw.client.post(
            "/v1/files/upload",
            files={"file": ("spec.md", doc1.encode("utf-8"), "text/markdown")},
            data={"workspace_id": "global"},
        )
        assert up1.status_code == 201
        up2 = await gw.client.post(
            "/v1/files/upload",
            files={"file": ("security.md", doc2.encode("utf-8"), "text/markdown")},
            data={"workspace_id": "global"},
        )
        assert up2.status_code == 201

        gw.llm.queue_response(
            MockLLMResponse.tool_call(
                name="knowledge_search",
                arguments={"query": "streaming support and key logging rules", "limit": 10},
                call_id="call_rag_search_2",
            )
        )
        gw.llm.queue_text_response(
            "According to [spec.md], /v1/chat/completions supports streaming. "
            "Additionally, per [security.md], keys must not be logged."
        )

        resp = await gw.client.post(
            "/v1/chat/completions",
            json={
                "model": "default",
                "messages": [{"role": "user", "content": "Summarize streaming and logging rules."}],
            },
        )
        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "[spec.md]" in content
        assert "[security.md]" in content

        # The retrieval tool result blended BOTH document sources
        assert gw.llm.call_count == 2
        tool_msgs = [
            m for m in gw.llm.recorded_requests[1].messages if m.get("role") == "tool"
        ]
        result_payload = tool_msgs[0]["content"]
        assert "spec.md" in result_payload
        assert "security.md" in result_payload
