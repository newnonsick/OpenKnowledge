

from abc import ABC, abstractmethod
from typing import List, Optional
from uuid import UUID
from src.gateway.domain.canonical import RankedSearchResult
from src.gateway.domain.entities import (
    DocumentChunk,
    DocumentFile,
    KnowledgeItem,
    KnowledgeRevision,
    Workspace,
)

class IKnowledgeRepository(ABC):

    @abstractmethod
    async def create_item(self, item: KnowledgeItem, initial_revision: KnowledgeRevision) -> KnowledgeItem:

        ...

    @abstractmethod
    async def get_item_by_id(
        self,
        item_id: UUID,
        version: Optional[int] = None,
        workspace_id: Optional[str] = None,
    ) -> Optional[KnowledgeItem]:

        ...

    @abstractmethod
    async def update_item_occ(
        self,
        item_id: UUID,
        expected_version: int,
        new_revision: KnowledgeRevision,
        title: Optional[str] = None,
        tags: Optional[List[str]] = None,
        is_global: Optional[bool] = None,
        workspace_id: Optional[str] = None,
    ) -> KnowledgeItem:

        ...

    @abstractmethod
    async def soft_delete_item(
        self,
        item_id: UUID,
        expected_version: Optional[int] = None,
        workspace_id: Optional[str] = None,
    ) -> bool:

        ...

    @abstractmethod
    async def list_revisions(self, item_id: UUID) -> List[KnowledgeRevision]:

        ...

    @abstractmethod
    async def search_fts(
        self,
        query: str,
        workspace_id: str,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

        ...

    @abstractmethod
    async def search_vector(
        self,
        query_vector: List[float],
        workspace_id: str,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

        ...

class IDocumentRepository(ABC):

    @abstractmethod
    async def save_document(self, document: DocumentFile) -> DocumentFile:

        ...

    @abstractmethod
    async def get_by_hash(self, workspace_id: str, content_hash: str) -> Optional[DocumentFile]:

        ...

    @abstractmethod
    async def get_by_id(self, document_id: UUID) -> Optional[DocumentFile]:

        ...

    @abstractmethod
    async def save_chunks_batch(self, chunks: List[DocumentChunk]) -> int:

        ...

    @abstractmethod
    async def get_chunks_by_document(self, document_id: UUID) -> List[DocumentChunk]:

        ...

    @abstractmethod
    async def search_chunks_fts(
        self,
        query: str,
        workspace_id: str,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

        ...

    @abstractmethod
    async def search_chunks_vector(
        self,
        query_vector: List[float],
        workspace_id: str,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

        ...

class IWorkspaceRepository(ABC):

    @abstractmethod
    async def ensure_workspace(self, workspace_id: str, name: Optional[str] = None) -> Workspace:

        ...

    @abstractmethod
    async def get_workspace(self, workspace_id: str) -> Optional[Workspace]:

        ...

    @abstractmethod
    async def list_workspaces(self) -> List[Workspace]:

        ...
