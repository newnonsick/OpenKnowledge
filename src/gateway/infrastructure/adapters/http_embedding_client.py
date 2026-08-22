

from __future__ import annotations

import asyncio
import logging
from time import perf_counter
from typing import Any, Dict, List, Optional
import httpx

from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import EmbeddingException
from src.gateway.infrastructure.adapters.upstream_resilience import (
    BulkheadRejectedError,
    CircuitOpenError,
    ResiliencePolicy,
    RetryableStatusError,
    shared_upstream_resilience,
)
from src.gateway.observability import current_trace_headers, increment_metric, observe_metric

logger = logging.getLogger(__name__)

class HTTPEmbeddingClient(IEmbeddingClient):

    _shared_client: Optional[httpx.AsyncClient] = None
    _shared_loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    async def get_shared_client(cls, timeout_seconds: float = 30.0) -> httpx.AsyncClient:

        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if (
            cls._shared_client is None
            or cls._shared_client.is_closed
            or cls._shared_loop != current_loop
        ):
            if cls._shared_client is not None and not cls._shared_client.is_closed:
                try:
                    await cls._shared_client.aclose()
                except Exception:
                    pass
            cls._shared_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    timeout=timeout_seconds,
                    connect=15.0,
                    read=timeout_seconds,
                    write=15.0,
                    pool=15.0,
                ),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50, keepalive_expiry=60.0),
            )
            cls._shared_loop = current_loop
        return cls._shared_client

    @classmethod
    async def close_shared_client(cls) -> None:

        if cls._shared_client is not None and not cls._shared_client.is_closed:
            await cls._shared_client.aclose()
            cls._shared_client = None
            cls._shared_loop = None

    def __init__(
        self,
        base_url: Optional[str] = None,
        model_id: Optional[str] = None,
        api_key: Optional[str] = None,
        dimension: Optional[int] = None,
        batch_size: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        resilience_policy: ResiliencePolicy | None = None,
    ) -> None:

        emb_cfg = get_settings().embedding
        self.base_url = (base_url or emb_cfg.url).rstrip("/")
        self.model_id = model_id or emb_cfg.model_id
        self.api_key = api_key or emb_cfg.api_key
        self._dimension = dimension or emb_cfg.dimension
        self.batch_size = max(1, batch_size if batch_size is not None else (emb_cfg.batch_size or 32))
        self.timeout_seconds = timeout_seconds or emb_cfg.timeout_seconds
        self._client = http_client
        self._resilience_policy = resilience_policy or ResiliencePolicy(
            max_attempts=emb_cfg.retry_attempts,
            base_backoff_seconds=emb_cfg.retry_backoff_seconds,
            max_backoff_seconds=max(emb_cfg.retry_backoff_seconds, emb_cfg.retry_backoff_seconds * 4),
            max_concurrency=emb_cfg.max_concurrency,
            bulkhead_timeout_seconds=emb_cfg.bulkhead_timeout_seconds,
            circuit_failure_threshold=emb_cfg.circuit_failure_threshold,
            circuit_recovery_seconds=emb_cfg.circuit_recovery_seconds,
            total_timeout_seconds=self.timeout_seconds,
        )

    @property
    def dimension(self) -> int:

        return self._dimension

    def _resolve_endpoint(self) -> str:

        if self.base_url.endswith("/embeddings") or self.base_url.endswith("/embed"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/embeddings"
        return f"{self.base_url}/v1/embeddings"

    def _build_headers(self) -> Dict[str, str]:

        headers = {"Content-Type": "application/json"}
        headers.update(current_trace_headers())
        if self.api_key and self.api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def embed_texts(self, texts: List[str]) -> List[List[float]]:

        if not texts:
            return []

        endpoint = self._resolve_endpoint()
        headers = self._build_headers()
        all_embeddings: List[List[float]] = []

        batches = [
            texts[i : i + self.batch_size]
            for i in range(0, len(texts), self.batch_size)
        ]

        async def _process_batch(client: httpx.AsyncClient, batch: List[str]) -> List[List[float]]:
            payload = {
                "input": batch,
                "model": self.model_id,
            }
            async def send() -> httpx.Response:
                response = await client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                if response.status_code in {408, 429} or response.status_code >= 500:
                    raise RetryableStatusError(response.status_code)
                return response

            guard = shared_upstream_resilience("embedding", self._resilience_policy)
            try:
                resp = await guard.call(
                    send,
                    retry_if=lambda exc: isinstance(exc, (httpx.RequestError, RetryableStatusError)),
                )
            except RetryableStatusError as exc:
                raise EmbeddingException(
                    message="Embedding provider request failed.",
                    details={"status_code": exc.status_code},
                ) from exc
            except (httpx.RequestError, TimeoutError, BulkheadRejectedError, CircuitOpenError) as exc:
                raise EmbeddingException(
                    message="Embedding provider connection failed.",
                    details={"error_type": type(exc).__name__},
                ) from exc

            if 400 <= resp.status_code < 500:
                raise EmbeddingException(
                    message="Embedding provider rejected the request.",
                    details={"status_code": resp.status_code},
                )
            data = resp.json()

            batch_vectors: List[List[float]] = []
            if isinstance(data, dict) and "data" in data:
                items = data["data"]

                if items and isinstance(items[0], dict) and "index" in items[0]:
                    items = sorted(items, key=lambda x: x.get("index", 0))
                for item in items:
                    if isinstance(item, dict) and "embedding" in item:
                        batch_vectors.append(item["embedding"])
                    elif isinstance(item, list):
                        batch_vectors.append(item)

            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "embedding" in item:
                        batch_vectors.append(item["embedding"])
                    elif isinstance(item, list):
                        batch_vectors.append(item)

            for vec in batch_vectors:
                if len(vec) != self._dimension:
                    raise EmbeddingException(
                        message=f"Dimension mismatch: expected {self._dimension}, got {len(vec)}",
                        details={"expected_dim": self._dimension, "actual_dim": len(vec)},
                    )

            if len(batch_vectors) != len(batch):
                raise EmbeddingException(
                    message=f"Embedding provider returned {len(batch_vectors)} vectors for {len(batch)} inputs.",
                    details={"expected_count": len(batch), "received_count": len(batch_vectors)},
                )

            return batch_vectors

        client = self._client or await self.get_shared_client(self.timeout_seconds)
        for b in batches:
            started = perf_counter()
            outcome = "success"
            try:
                res = await _process_batch(client, b)
            except Exception as exc:
                outcome = "timeout" if isinstance(exc.__cause__, (TimeoutError, httpx.TimeoutException)) else "error"
                raise
            finally:
                elapsed = perf_counter() - started
                increment_metric("gateway_embedding_events_total", event="batch", outcome=outcome)
                observe_metric("gateway_dependency_duration_seconds", elapsed, dependency="embedding", operation="batch", outcome=outcome)
                observe_metric("gateway_embedding_batch_size", len(b), operation="batch", outcome=outcome)
            all_embeddings.extend(res)

        return all_embeddings

    async def embed_query(self, query: str) -> List[float]:

        results = await self.embed_texts([query])
        if not results:
            raise EmbeddingException("Empty embedding result received for query.")
        return results[0]
