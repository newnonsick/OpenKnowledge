"""
Mock HTTP Server Harness for OpenKnowledge E2E Test Suite.

Provides standalone and in-process emulation for:
- Upstream LLM Server (OpenAI / vLLM / Ollama chat completions in JSON and SSE modes).
- Upstream Anthropic Messages Server emulation.
- Upstream Embedding Server (OpenAI / TEI embeddings with deterministic unit-normalized vectors).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Literal, Optional, Sequence, Tuple, Union

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


# ==============================================================================
# 1. Domain Models & Recorded Request Data Structures
# ==============================================================================

@dataclass
class RecordedRequest:
    """Snapshot of an incoming HTTP request received by the mock server."""
    endpoint: str
    method: str
    headers: Dict[str, str]
    body: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    @property
    def messages(self) -> List[Dict[str, Any]]:
        return self.body.get("messages", [])

    @property
    def tools(self) -> List[Dict[str, Any]]:
        return self.body.get("tools", [])

    @property
    def model(self) -> str:
        return self.body.get("model", "")

    @property
    def stream(self) -> bool:
        return bool(self.body.get("stream", False))

    @property
    def inputs(self) -> List[str]:
        raw_input = self.body.get("input", [])
        if isinstance(raw_input, str):
            return [raw_input]
        return list(raw_input)

    def get_last_user_message(self) -> str:
        for msg in reversed(self.messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                elif isinstance(content, list):
                    return " ".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
        return ""


@dataclass
class MockToolCall:
    name: str
    arguments: Union[Dict[str, Any], str]
    id: Optional[str] = None
    tool_type: str = "function"

    def to_openai_dict(self) -> Dict[str, Any]:
        args_str = (
            self.arguments
            if isinstance(self.arguments, str)
            else json.dumps(self.arguments, ensure_ascii=False)
        )
        return {
            "id": self.id or f"call_{uuid.uuid4().hex[:12]}",
            "type": self.tool_type,
            "function": {
                "name": self.name,
                "arguments": args_str,
            },
        }

    def to_anthropic_content_block(self) -> Dict[str, Any]:
        args_dict = (
            json.loads(self.arguments)
            if isinstance(self.arguments, str)
            else self.arguments
        )
        return {
            "type": "tool_use",
            "id": self.id or f"toolu_{uuid.uuid4().hex[:12]}",
            "name": self.name,
            "input": args_dict,
        }


@dataclass
class MockLLMResponse:
    """Pre-programmed response definition for the mock LLM."""
    content: Optional[str] = None
    reasoning_content: Optional[str] = None
    tool_calls: Optional[List[MockToolCall]] = None
    finish_reason: Optional[str] = "stop"
    status_code: int = 200
    error_detail: Optional[Dict[str, Any]] = None
    stream_chunks: Optional[List[str]] = None
    stream_reasoning_chunks: Optional[List[str]] = None
    delay_seconds: float = 0.0
    prompt_tokens: int = 25
    completion_tokens: int = 15

    @classmethod
    def text(cls, content: str, finish_reason: str = "stop", **kwargs) -> MockLLMResponse:
        return cls(content=content, finish_reason=finish_reason, **kwargs)

    @classmethod
    def tool_call(
        cls,
        name: str,
        arguments: Union[Dict[str, Any], str],
        call_id: Optional[str] = None,
        content: Optional[str] = None,
        **kwargs,
    ) -> MockLLMResponse:
        tc = MockToolCall(name=name, arguments=arguments, id=call_id)
        return cls(content=content, tool_calls=[tc], finish_reason="tool_calls", **kwargs)

    @classmethod
    def multiple_tool_calls(
        cls,
        tools: List[MockToolCall],
        content: Optional[str] = None,
        **kwargs,
    ) -> MockLLMResponse:
        return cls(content=content, tool_calls=tools, finish_reason="tool_calls", **kwargs)

    @classmethod
    def stream(
        cls,
        chunks: List[str],
        delay_seconds: float = 0.0,
        finish_reason: str = "stop",
        reasoning_content: Optional[str] = None,
        **kwargs,
    ) -> MockLLMResponse:
        return cls(
            stream_chunks=chunks,
            delay_seconds=delay_seconds,
            finish_reason=finish_reason,
            reasoning_content=reasoning_content,
            **kwargs,
        )

    @classmethod
    def error(
        cls,
        status_code: int,
        message: str,
        error_type: str = "invalid_request_error",
    ) -> MockLLMResponse:
        return cls(
            status_code=status_code,
            error_detail={"error": {"message": message, "type": error_type, "code": status_code}},
        )


# ==============================================================================
# 2. Deterministic Vector Generator (Embedding Engine)
# ==============================================================================

class DeterministicEmbeddingEngine:
    """Generates reproducible, L2-normalized float vectors from input strings."""

    def __init__(self, dimension: int = 1024):
        self.dimension = dimension
        self.custom_embeddings: Dict[str, List[float]] = {}

    def set_dimension(self, dimension: int) -> None:
        self.dimension = dimension

    def register_custom_vector(self, text: str, vector: List[float]) -> None:
        """Register a known vector for a specific text string."""
        if len(vector) != self.dimension:
            raise ValueError(
                f"Vector dimension {len(vector)} does not match engine dimension {self.dimension}"
            )
        self.custom_embeddings[text] = vector

    def generate_vector(self, text: str) -> List[float]:
        """Generate deterministic L2-normalized vector for given text."""
        if text in self.custom_embeddings:
            return self.custom_embeddings[text]

        # Seed PRNG using SHA-256 hash of text
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], byteorder="big")
        rng = random.Random(seed)

        # Generate Gaussian random vector
        raw_vector = [rng.gauss(0.0, 1.0) for _ in range(self.dimension)]

        # L2-normalization to ensure unit norm: ||v|| = 1.0
        norm = math.sqrt(sum(x * x for x in raw_vector))
        if norm == 0.0:
            return [0.0] * self.dimension

        return [round(x / norm, 6) for x in raw_vector]


# ==============================================================================
# 3. Upstream Controllers (LLM & Embedding Mock Controllers)
# ==============================================================================

class MockLLMController:
    """Manages programmed responses, rules, and call histories for LLM emulation."""

    def __init__(self):
        self.response_queue: asyncio.Queue[MockLLMResponse] = asyncio.Queue()
        self.rules: List[
            Tuple[Callable[[RecordedRequest], bool], Callable[[RecordedRequest], MockLLMResponse]]
        ] = []
        self.recorded_requests: List[RecordedRequest] = []
        self.default_model_id: str = "meta-llama/Llama-3.1-8B-Instruct"

    @property
    def call_count(self) -> int:
        return len(self.recorded_requests)

    @property
    def last_request(self) -> Optional[RecordedRequest]:
        return self.recorded_requests[-1] if self.recorded_requests else None

    def queue_response(self, response: MockLLMResponse) -> None:
        self.response_queue.put_nowait(response)

    def queue_text_response(self, text: str, finish_reason: str = "stop", **kwargs) -> None:
        self.queue_response(MockLLMResponse.text(text, finish_reason=finish_reason, **kwargs))

    def queue_tool_call(
        self,
        name: str,
        arguments: Union[Dict[str, Any], str],
        call_id: Optional[str] = None,
        content: Optional[str] = None,
        **kwargs,
    ) -> None:
        self.queue_response(
            MockLLMResponse.tool_call(
                name=name, arguments=arguments, call_id=call_id, content=content, **kwargs
            )
        )

    def queue_multiple_tool_calls(self, tools: List[MockToolCall], **kwargs) -> None:
        self.queue_response(MockLLMResponse.multiple_tool_calls(tools, **kwargs))

    def queue_stream_response(
        self,
        chunks: List[str],
        delay_seconds: float = 0.0,
        finish_reason: str = "stop",
    ) -> None:
        self.queue_response(
            MockLLMResponse.stream(
                chunks, delay_seconds=delay_seconds, finish_reason=finish_reason
            )
        )

    def queue_error(self, status_code: int, message: str) -> None:
        self.queue_response(MockLLMResponse.error(status_code, message))

    def add_rule(
        self,
        match_fn: Callable[[RecordedRequest], bool],
        response_fn: Callable[[RecordedRequest], MockLLMResponse],
    ) -> None:
        self.rules.append((match_fn, response_fn))

    def reset(self) -> None:
        self.response_queue = asyncio.Queue()
        self.rules.clear()
        self.recorded_requests.clear()

    async def get_next_response(self, req: RecordedRequest) -> MockLLMResponse:
        self.recorded_requests.append(req)

        # 1. Check if an explicitly queued response exists
        if not self.response_queue.empty():
            return await self.response_queue.get()

        # 2. Check rule matchers in order
        for match_fn, response_fn in self.rules:
            if match_fn(req):
                return response_fn(req)

        # 3. Default fallback response
        user_msg = req.get_last_user_message()
        fallback_text = (
            f"Mock LLM response to: {user_msg}"
            if user_msg
            else "Mock LLM default completion."
        )
        return MockLLMResponse.text(fallback_text)


class MockEmbeddingController:
    """Manages embedding generation, vector dimensions, and error simulation."""

    def __init__(self, dimension: int = 1024):
        self.engine = DeterministicEmbeddingEngine(dimension=dimension)
        self.recorded_requests: List[RecordedRequest] = []
        self.error_queue: asyncio.Queue[Tuple[int, str]] = asyncio.Queue()
        self.default_model_id: str = "BAAI/bge-large-en-v1.5"

    @property
    def dimension(self) -> int:
        return self.engine.dimension

    def set_dimension(self, dimension: int) -> None:
        self.engine.set_dimension(dimension)

    @property
    def call_count(self) -> int:
        return len(self.recorded_requests)

    @property
    def last_request(self) -> Optional[RecordedRequest]:
        return self.recorded_requests[-1] if self.recorded_requests else None

    def queue_error(self, status_code: int, message: str) -> None:
        self.error_queue.put_nowait((status_code, message))

    def register_custom_vector(self, text: str, vector: List[float]) -> None:
        self.engine.register_custom_vector(text, vector)

    def reset(self) -> None:
        self.recorded_requests.clear()
        self.error_queue = asyncio.Queue()
        self.engine.custom_embeddings.clear()

    async def process_embeddings(self, req: RecordedRequest) -> Tuple[int, Dict[str, Any]]:
        self.recorded_requests.append(req)

        # Check for queued errors
        if not self.error_queue.empty():
            status_code, msg = await self.error_queue.get()
            return status_code, {
                "error": {"message": msg, "type": "api_error", "code": status_code}
            }

        inputs = req.inputs
        model_name = req.model or self.default_model_id

        data_items = []
        total_tokens = 0
        for idx, text in enumerate(inputs):
            vec = self.engine.generate_vector(text)
            data_items.append({
                "object": "embedding",
                "index": idx,
                "embedding": vec,
            })
            total_tokens += max(1, len(text) // 4)

        return 200, {
            "object": "list",
            "data": data_items,
            "model": model_name,
            "usage": {
                "prompt_tokens": total_tokens,
                "total_tokens": total_tokens,
            },
        }


# ==============================================================================
# 4. Starlette / ASGI Application Factory
# ==============================================================================

def create_mock_upstream_app(
    llm_controller: MockLLMController,
    embedding_controller: MockEmbeddingController,
) -> Starlette:
    """Build the ASGI application exposing LLM and Embedding mock routes."""

    async def handle_health(request: Request) -> Response:
        return JSONResponse({"status": "ok", "service": "mock-upstream-harness"})

    async def handle_chat_completions(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                status_code=400,
            )

        headers = dict(request.headers)
        recorded_req = RecordedRequest(
            endpoint="/v1/chat/completions",
            method="POST",
            headers=headers,
            body=body,
        )

        mock_resp = await llm_controller.get_next_response(recorded_req)

        # Handle programmed errors
        if mock_resp.status_code != 200:
            error_payload = mock_resp.error_detail or {"error": {"message": "Simulated error"}}
            return JSONResponse(error_payload, status_code=mock_resp.status_code)

        # Handle SSE Streaming Mode
        if recorded_req.stream or mock_resp.stream_chunks is not None:
            return _build_sse_streaming_response(recorded_req, mock_resp)

        # Handle Non-Streaming JSON Mode
        return _build_json_completion_response(recorded_req, mock_resp)

    async def handle_anthropic_messages(request: Request) -> Response:
        """Handle Anthropic format requests if upstream provides Anthropic emulation."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                status_code=400,
            )

        headers = dict(request.headers)
        recorded_req = RecordedRequest(
            endpoint="/v1/messages",
            method="POST",
            headers=headers,
            body=body,
        )

        mock_resp = await llm_controller.get_next_response(recorded_req)

        if mock_resp.status_code != 200:
            error_payload = mock_resp.error_detail or {"error": {"message": "Simulated error"}}
            return JSONResponse(error_payload, status_code=mock_resp.status_code)

        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        model_name = recorded_req.model or llm_controller.default_model_id

        content_blocks = []
        if mock_resp.content:
            content_blocks.append({"type": "text", "text": mock_resp.content})
        if mock_resp.tool_calls:
            for tc in mock_resp.tool_calls:
                content_blocks.append(tc.to_anthropic_content_block())

        if not content_blocks:
            content_blocks.append({"type": "text", "text": "Mock Anthropic response."})

        stop_reason = (
            "tool_use"
            if mock_resp.tool_calls
            else ("end_turn" if mock_resp.finish_reason == "stop" else mock_resp.finish_reason)
        )

        payload = {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": content_blocks,
            "model": model_name,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": mock_resp.prompt_tokens,
                "output_tokens": mock_resp.completion_tokens,
            },
        }
        return JSONResponse(payload, status_code=200)

    async def handle_embeddings(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                status_code=400,
            )

        headers = dict(request.headers)
        recorded_req = RecordedRequest(
            endpoint=request.url.path,
            method="POST",
            headers=headers,
            body=body,
        )

        status_code, payload = await embedding_controller.process_embeddings(recorded_req)
        return JSONResponse(payload, status_code=status_code)

    def _build_json_completion_response(
        req: RecordedRequest, resp: MockLLMResponse
    ) -> Response:
        completion_id = f"chatcmpl-mock-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())
        model_name = req.model or llm_controller.default_model_id

        tool_calls_payload = None
        if resp.tool_calls:
            tool_calls_payload = [tc.to_openai_dict() for tc in resp.tool_calls]

        choice_message: Dict[str, Any] = {
            "role": "assistant",
            "content": resp.content,
        }
        if resp.reasoning_content is not None:
            choice_message["reasoning_content"] = resp.reasoning_content
        if tool_calls_payload is not None:
            choice_message["tool_calls"] = tool_calls_payload

        payload = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created_ts,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": choice_message,
                    "finish_reason": resp.finish_reason
                    or ("tool_calls" if tool_calls_payload else "stop"),
                }
            ],
            "usage": {
                "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens,
                "total_tokens": resp.prompt_tokens + resp.completion_tokens,
            },
            "system_fingerprint": "fp_mock_llm",
        }
        return JSONResponse(payload, status_code=200)

    def _build_sse_streaming_response(
        req: RecordedRequest, resp: MockLLMResponse
    ) -> StreamingResponse:
        completion_id = f"chatcmpl-mock-stream-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())
        model_name = req.model or llm_controller.default_model_id

        async def sse_event_generator() -> AsyncIterator[str]:
            # Initial role chunk
            first_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(first_chunk)}\n\n"

            # 0. Stream reasoning deltas first (reasoning models emit thinking before content)
            reasoning_chunks = (
                resp.stream_reasoning_chunks
                if resp.stream_reasoning_chunks is not None
                else ([resp.reasoning_content] if resp.reasoning_content else [])
            )
            for reasoning_chunk in reasoning_chunks:
                if resp.delay_seconds > 0:
                    await asyncio.sleep(resp.delay_seconds)
                reasoning_payload = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning_content": reasoning_chunk},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(reasoning_payload)}\n\n"

            # 1. If tool calls exist, stream tool call chunks
            if resp.tool_calls:
                for tc_idx, tc in enumerate(resp.tool_calls):
                    tc_dict = tc.to_openai_dict()
                    tc_chunk = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model_name,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": tc_idx,
                                            "id": tc_dict["id"],
                                            "type": "function",
                                            "function": {
                                                "name": tc_dict["function"]["name"],
                                                "arguments": tc_dict["function"]["arguments"],
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(tc_chunk)}\n\n"

                final_tc_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": resp.finish_reason or "tool_calls",
                        }
                    ],
                }
                yield f"data: {json.dumps(final_tc_chunk)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # 2. If stream chunks provided, yield chunks
            chunks = (
                resp.stream_chunks
                if resp.stream_chunks is not None
                else ([resp.content] if resp.content else ["Hello from mock"])
            )
            for text_chunk in chunks:
                if resp.delay_seconds > 0:
                    await asyncio.sleep(resp.delay_seconds)
                chunk_payload = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_ts,
                    "model": model_name,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text_chunk},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk_payload)}\n\n"

            # Final finish reason chunk
            finish_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": resp.finish_reason or "stop",
                    }
                ],
            }
            yield f"data: {json.dumps(finish_chunk)}\n\n"

            # Done event
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse_event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    routes = [
        Route("/health", handle_health, methods=["GET"]),
        Route("/v1/chat/completions", handle_chat_completions, methods=["POST"]),
        Route("/v1/messages", handle_anthropic_messages, methods=["POST"]),
        Route("/v1/embeddings", handle_embeddings, methods=["POST"]),
        Route("/embed", handle_embeddings, methods=["POST"]),  # TEI compatibility
    ]

    return Starlette(debug=True, routes=routes)


# ==============================================================================
# 5. Standalone Server Manager & Test Fixture Harness
# ==============================================================================

class MockServerManager:
    """
    High-level test harness managing the lifecycle of the mock upstream server.
    Can run in-process or spin up an ephemeral background Uvicorn socket server.
    """

    def __init__(self, embedding_dimension: int = 1024):
        self.llm = MockLLMController()
        self.embedding = MockEmbeddingController(dimension=embedding_dimension)
        self.app = create_mock_upstream_app(self.llm, self.embedding)
        self._server_task: Optional[asyncio.Task] = None
        self._server_instance: Optional[Any] = None
        self.host: str = "127.0.0.1"
        self.port: int = 0
        self.base_url: str = ""

    @property
    def llm_url(self) -> str:
        return f"{self.base_url}/v1" if self.base_url else "http://mock-upstream/v1"

    @property
    def embedding_url(self) -> str:
        return f"{self.base_url}/v1" if self.base_url else "http://mock-upstream/v1"

    def reset(self) -> None:
        """Reset state across test iterations."""
        self.llm.reset()
        self.embedding.reset()

    async def start(self) -> MockServerManager:
        """Start the mock server on an ephemeral OS-assigned port (Port 0)."""
        import uvicorn

        config = uvicorn.Config(
            app=self.app,
            host=self.host,
            port=0,
            log_level="error",
            lifespan="on",
        )
        server = uvicorn.Server(config)
        self._server_instance = server

        # Run server startup in background task
        self._server_task = asyncio.create_task(server.serve())

        # Wait until server is listening and port is bound
        while not server.started:
            await asyncio.sleep(0.01)

        # Retrieve ephemeral port assigned by OS
        assert len(server.servers) > 0 and len(server.servers[0].sockets) > 0
        sock = server.servers[0].sockets[0]
        self.port = sock.getsockname()[1]
        self.base_url = f"http://{self.host}:{self.port}"
        return self

    async def stop(self) -> None:
        """Gracefully terminate background server task."""
        if self._server_instance:
            self._server_instance.should_exit = True
        if self._server_task:
            await self._server_task
            self._server_task = None
        self._server_instance = None

    async def __aenter__(self) -> MockServerManager:
        return await self.start()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.stop()
