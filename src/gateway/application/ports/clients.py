

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, List, Optional
from src.gateway.domain.canonical import CanonicalLLMResponse, CanonicalLLMStreamChunk

class ILLMClient(ABC):

    @abstractmethod
    async def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> CanonicalLLMResponse:

        ...

    @abstractmethod
    async def generate_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> AsyncIterator[CanonicalLLMStreamChunk]:

        ...

class IEmbeddingClient(ABC):

    @abstractmethod
    async def embed_texts(self, texts: List[str]) -> List[List[float]]:

        ...

    @abstractmethod
    async def embed_query(self, query: str) -> List[float]:

        ...

    @property
    @abstractmethod
    def dimension(self) -> int:

        ...
