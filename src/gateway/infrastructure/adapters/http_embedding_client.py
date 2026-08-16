

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
import httpx

from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import EmbeddingException

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
    ) -> None:

        emb_cfg = get_settings().embedding
        self.base_url = (base_url or emb_cfg.url).rstrip("/")
        self.model_id = model_id or emb_cfg.model_id
        self.api_key = api_key or emb_cfg.api_key
        self._dimension = dimension or emb_cfg.dimension
        self.batch_size = max(1, batch_size if batch_size is not None else (emb_cfg.batch_size or 32))
        self.timeout_seconds = timeout_seconds or emb_cfg.timeout_seconds
        self._client = http_client

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
            try:
                resp = await client.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except Exception as exc:
                raise EmbeddingException(
                    message=f"Failed to connect to embedding service at {endpoint}: {exc}",
                    details={"error": str(exc), "endpoint": endpoint},
                ) from exc

            if resp.status_code != 200:
                try:
                    err_json = resp.json()
                except Exception:
                    err_json = {"raw": resp.text}
                raise EmbeddingException(
                    message=f"Embedding provider returned HTTP {resp.status_code}: {resp.text}",
                    details={"status_code": resp.status_code, "response": err_json},
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
            res = await _process_batch(client, b)
            all_embeddings.extend(res)

        return all_embeddings

    async def embed_query(self, query: str) -> List[float]:

        results = await self.embed_texts([query])
        if not results:
            raise EmbeddingException("Empty embedding result received for query.")
        return results[0]
