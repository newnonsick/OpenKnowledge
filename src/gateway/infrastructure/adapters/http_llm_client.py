

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional
import httpx

from src.gateway.application.ports.clients import ILLMClient
from src.gateway.config import get_settings
from src.gateway.domain.canonical import (
    CanonicalLLMResponse,
    CanonicalLLMStreamChunk,
    CanonicalUsage,
)
from src.gateway.domain.exceptions import LLMProviderException
from src.gateway.domain.tools import FunctionCall, ToolCall

logger = logging.getLogger(__name__)

_FINISH_REASON_MAP = {
    "length": "max_tokens",
    "tool_calls": "tool_use",
}

def _normalize_finish_reason(raw: Optional[str]) -> Optional[str]:

    if not raw:
        return None
    return _FINISH_REASON_MAP.get(raw, raw)

class HttpLLMClient(ILLMClient):

    _shared_client: Optional[httpx.AsyncClient] = None
    _shared_loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    async def get_shared_client(cls, timeout_seconds: float = 120.0) -> httpx.AsyncClient:

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
                    connect=30.0,
                    read=timeout_seconds,
                    write=30.0,
                    pool=30.0,
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
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):

        current_settings = get_settings()
        raw_url = (base_url or current_settings.llm.url).rstrip("/")
        if raw_url.endswith("/chat/completions"):
            self.endpoint = raw_url
            self.base_url = raw_url.replace("/chat/completions", "")
        elif raw_url.endswith("/v1"):
            self.endpoint = f"{raw_url}/chat/completions"
            self.base_url = raw_url
        else:
            self.endpoint = f"{raw_url}/v1/chat/completions"
            self.base_url = f"{raw_url}/v1"

        self.api_key = api_key if api_key is not None else current_settings.llm.api_key
        self.default_model = (
            default_model if default_model is not None else current_settings.llm.model_id
        )
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else current_settings.llm.timeout_seconds
        )
        self._client = client

    def _get_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key and self.api_key != "EMPTY":
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return await self.get_shared_client(self.timeout_seconds)

    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> CanonicalLLMResponse:
        target_model = model or self.default_model

        formatted_messages = []
        for m in messages:
            if isinstance(m, dict):
                formatted_messages.append(m)
            elif hasattr(m, "role") and hasattr(m, "text_content"):
                formatted_messages.append({"role": m.role, "content": m.text_content})
            elif hasattr(m, "model_dump"):
                formatted_messages.append(m.model_dump())
            else:
                formatted_messages.append(m)

        payload: Dict[str, Any] = {
            "model": target_model,
            "messages": formatted_messages,
            "stream": False,
        }

        if tools:
            payload["tools"] = tools
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        for k, v in kwargs.items():
            if v is not None and k not in payload:
                payload[k] = v

        headers = self._get_headers()
        client = await self._get_client()

        req_timeout = httpx.Timeout(
            timeout=self.timeout_seconds,
            connect=30.0,
            read=self.timeout_seconds,
            write=30.0,
            pool=30.0,
        )

        try:
            resp = await client.post(
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=req_timeout,
            )

            if 400 <= resp.status_code < 500:
                logger.error(
                    "LLM provider rejected request",
                    extra={"status_code": resp.status_code},
                )
                raise LLMProviderException(
                    message="LLM provider rejected the request.",
                    details={"status_code": resp.status_code},
                )
            if resp.status_code >= 500:
                logger.error(
                    "LLM provider request failed",
                    extra={"status_code": resp.status_code},
                )
                raise LLMProviderException(
                    message="LLM provider request failed.",
                    details={"status_code": resp.status_code},
                )

            data = resp.json()

            choices = data.get("choices", [])
            content: Optional[str] = None
            reasoning_content: Optional[str] = None
            tool_calls: List[ToolCall] = []
            finish_reason = "stop"

            if choices:
                first_choice = choices[0]
                finish_reason = _normalize_finish_reason(first_choice.get("finish_reason")) or "stop"
                msg_payload = first_choice.get("message", {})
                content = msg_payload.get("content")
                reasoning_content = msg_payload.get("reasoning_content") or msg_payload.get("reasoning")

                raw_tool_calls = msg_payload.get("tool_calls", [])
                if raw_tool_calls:
                    for tc in raw_tool_calls:
                        fn = tc.get("function", {})
                        tool_calls.append(
                            ToolCall(
                                id=tc.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                                type="function",
                                index=tc.get("index"),
                                function=FunctionCall(
                                    name=fn.get("name", ""),
                                    arguments=fn.get("arguments", "{}"),
                                ),
                            )
                        )

            usage_data = data.get("usage", {})
            usage = CanonicalUsage(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get(
                    "total_tokens",
                    usage_data.get("prompt_tokens", 0) + usage_data.get("completion_tokens", 0),
                ),
            )

            return CanonicalLLMResponse(
                id=data.get("id", f"chatcmpl-{uuid.uuid4().hex[:12]}"),
                model=data.get("model", target_model),
                content=content,
                reasoning_content=reasoning_content,
                tool_calls=tool_calls,
                finish_reason=finish_reason or "stop",
                usage=usage,
            )

        except httpx.RequestError as exc:
            logger.error(
                "LLM provider connection failed",
                extra={"exception_class": type(exc).__name__},
            )
            raise LLMProviderException(
                message="LLM provider connection failed.",
                details={"error_type": type(exc).__name__},
            )

    async def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[CanonicalLLMStreamChunk]:

        target_model = model or self.default_model

        formatted_messages = []
        for m in messages:
            if isinstance(m, dict):
                formatted_messages.append(m)
            elif hasattr(m, "role") and hasattr(m, "text_content"):
                formatted_messages.append({"role": m.role, "content": m.text_content})
            elif hasattr(m, "model_dump"):
                formatted_messages.append(m.model_dump())
            else:
                formatted_messages.append(m)

        payload: Dict[str, Any] = {
            "model": target_model,
            "messages": formatted_messages,
            "stream": True,

            "stream_options": {"include_usage": True},
        }

        if tools:
            payload["tools"] = tools
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        for k, v in kwargs.items():
            if v is not None and k not in payload:
                payload[k] = v

        headers = self._get_headers()
        headers["Accept"] = "text/event-stream"

        client = await self._get_client()
        req_timeout = httpx.Timeout(
            timeout=self.timeout_seconds,
            connect=30.0,
            read=self.timeout_seconds,
            write=30.0,
            pool=30.0,
        )

        try:
            async with client.stream(
                "POST",
                self.endpoint,
                json=payload,
                headers=headers,
                timeout=req_timeout,
            ) as response:
                if 400 <= response.status_code < 500:
                    await response.aread()
                    raise LLMProviderException(
                        message="LLM provider rejected the streaming request.",
                        details={"status_code": response.status_code},
                    )
                if response.status_code >= 500:
                    await response.aread()
                    raise LLMProviderException(
                        message="LLM provider stream connection failed.",
                        details={"status_code": response.status_code},
                    )

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue

                    if line.startswith("data: "):
                        data_content = line[6:].strip()
                    elif line.startswith("data:"):
                        data_content = line[5:].strip()
                    else:
                        continue

                    if data_content == "[DONE]":
                        break

                    try:
                        chunk_dict = json.loads(data_content)
                    except json.JSONDecodeError:
                        logger.debug("Skipping malformed SSE chunk")
                        continue

                    choices = chunk_dict.get("choices", [])
                    delta_content: Optional[str] = None
                    delta_reasoning: Optional[str] = None
                    delta_tool_calls: Optional[List[ToolCall]] = None
                    finish_reason: Optional[str] = None

                    if choices:
                        choice = choices[0]
                        finish_reason = _normalize_finish_reason(choice.get("finish_reason"))
                        delta = choice.get("delta", {})
                        delta_content = delta.get("content")
                        delta_reasoning = delta.get("reasoning_content") or delta.get("reasoning")

                        raw_tcs = delta.get("tool_calls", [])
                        if raw_tcs:
                            delta_tool_calls = []
                            for tc in raw_tcs:
                                fn = tc.get("function", {})
                                delta_tool_calls.append(
                                    ToolCall(

                                        id=tc.get("id") or "",
                                        type="function",
                                        index=tc.get("index"),
                                        function=FunctionCall(
                                            name=fn.get("name", ""),
                                            arguments=fn.get("arguments", "") or "",
                                        ),
                                    )
                                )

                    usage_data = chunk_dict.get("usage")
                    usage_obj = None
                    if usage_data:
                        usage_obj = CanonicalUsage(
                            prompt_tokens=usage_data.get("prompt_tokens", 0),
                            completion_tokens=usage_data.get("completion_tokens", 0),
                            total_tokens=usage_data.get("total_tokens", 0),
                        )

                    yield CanonicalLLMStreamChunk(
                        id=chunk_dict.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}"),
                        model=chunk_dict.get("model", target_model),
                        delta_content=delta_content,
                        delta_reasoning_content=delta_reasoning,
                        delta_tool_calls=delta_tool_calls,
                        finish_reason=finish_reason,
                        usage=usage_obj,
                    )

        except httpx.RequestError as exc:
            logger.error(
                "LLM provider stream connection failed",
                extra={"exception_class": type(exc).__name__},
            )
            raise LLMProviderException(
                message="LLM provider stream connection failed.",
                details={"error_type": type(exc).__name__},
            )
