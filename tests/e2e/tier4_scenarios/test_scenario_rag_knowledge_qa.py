"""Tier 4 Real-World Scenario: RAG Knowledge Q&A Lifecycle.

Simulates an end-to-end developer question-answering workflow through the REAL
gateway stack: file upload -> parsing -> chunking -> mock embedding -> pgvector
indexing -> knowledge_search interception (hybrid FTS + vector + RRF) -> LLM
synthesis with citations.
"""

from uuid import UUID, uuid4

from sqlalchemy import select

import httpx
import pytest

from src.gateway.application.services.bounded_document_parser import BoundedDocumentParser
from src.gateway.application.services.document_ingestion_worker import DocumentIngestionWorker
from src.gateway.config import get_settings
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, IngestionJobModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from tests.e2e.harness.test_env import GatewayMockUpstream


async def _upload_and_ingest(gw, filename: str, content: str) -> dict:
    upload_resp = await gw.client.post(
        "/api/v1/sources/upload",
        files={"file": (filename, content.encode("utf-8"), "text/markdown")},
        data={"space_id": "global"},
        headers={"Idempotency-Key": f"rag-{filename}-{uuid4()}"},
    )
    assert upload_resp.status_code == 202, upload_resp.text
    receipt = upload_resp.json()
    assert receipt["job_state"] == "queued"

    async with gw.env.session_factory.begin() as session:
        generation = await session.scalar(
            select(EmbeddingGenerationModel).where(EmbeddingGenerationModel.purpose == "retrieval")
        )
        if generation is None:
            session.add(
                EmbeddingGenerationModel(
                    purpose="retrieval",
                    model_id=get_settings().embedding.model_id,
                    dimensions=gw.embedding.dimension,
                    status="active",
                )
            )

    settings = get_settings()
    worker = DocumentIngestionWorker(
        gw.env.session_factory,
        LocalVersionedObjectStorage(settings.gateway.storage_dir),
        BoundedDocumentParser(
            timeout_seconds=settings.gateway.parser_timeout_seconds,
            memory_limit_bytes=settings.gateway.parser_memory_limit_bytes,
            cpu_seconds=settings.gateway.parser_cpu_seconds,
            max_pages=settings.gateway.parser_max_pages,
            max_output_characters=settings.gateway.parser_max_output_characters,
        ),
        HTTPEmbeddingClient(),
        worker_id=f"e2e-{filename}",
        lease_seconds=30,
        heartbeat_interval_seconds=1,
        chunk_size=300,
        chunk_overlap=30,
        max_chunks=20,
    )
    assert await worker.run_once() == UUID(receipt["job_id"])

    async with gw.env.session_factory() as session:
        job = await session.get(IngestionJobModel, UUID(receipt["job_id"]))
        assert job is not None
        assert job.state == "succeeded", job.last_error_code
    return receipt


@pytest.mark.tier4
@pytest.mark.asyncio
async def test_scenario_f16_f20_f21_f22_f23_rag_knowledge_qa_complete_lifecycle():
    """Scenario: upload an architecture doc, ask a question, gateway intercepts
    knowledge_search, retrieves the ingested chunks, and synthesizes an answer."""
    from tests.e2e.harness.mock_server import MockLLMResponse

    async with GatewayMockUpstream(embedding_dimension=EMBED_DIM) as gw:
        doc_text = (
            "# Gateway Authentication Architecture\n\n"
            "## Token Validation Flow\n"
            "The gateway verifies Bearer tokens and x-api-key headers using constant-time comparison. "
            "All configured keys in GATEWAY_API_KEYS are granted full access across all workspace scopes.\n\n"
            "## OCC Version Control\n"
            "Knowledge updates enforce Optimistic Concurrency Control by comparing expected_version "
            "against the latest persisted revision version in the database."
        )

        await _upload_and_ingest(gw, "auth_architecture.md", doc_text)

        # 2. Ask a question; the model responds with an internal knowledge_search call
        gw.llm.queue_response(
            MockLLMResponse.tool_call(
                name="knowledge_search",
                arguments={
                    "query": (
                        "The gateway verifies Bearer tokens and x-api-key headers "
                        "using constant-time comparison."
                    )
                },
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
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "How are Bearer tokens and x-api-key headers compared?"
                        ),
                    }
                ],
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

    async with GatewayMockUpstream(embedding_dimension=EMBED_DIM) as gw:
        doc1 = (
            "Gateway Feature Policy: streaming support and key logging rules. "
            "API Specification: POST /v1/chat/completions supports streaming SSE "
            "responses for real-time token delivery."
        )
        doc2 = (
            "Gateway Feature Policy: streaming support and key logging rules. "
            "Security Policy: Passwords and API keys must never be logged in plain text. "
            "Rotate gateway keys every 90 days."
        )

        await _upload_and_ingest(gw, "spec.md", doc1)
        await _upload_and_ingest(gw, "security.md", doc2)

        gw.llm.queue_response(
            MockLLMResponse.tool_call(
                name="knowledge_search",
                arguments={
                    "query": "Gateway Feature Policy streaming support key logging rules",
                    "limit": 10,
                },
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
