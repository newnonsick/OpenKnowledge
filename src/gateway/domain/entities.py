

from datetime import datetime, timezone
import hashlib
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4
from pydantic import BaseModel, Field

from src.gateway.config import settings

def utc_now() -> datetime:

    return datetime.now(timezone.utc)

class Workspace(BaseModel):

    id: str
    name: str = "Workspace"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

class KnowledgeRevision(BaseModel):

    id: UUID = Field(default_factory=uuid4)
    item_id: UUID
    version: int = 1
    title: Optional[str] = None
    content: str
    content_hash: str
    tags: List[str] = Field(default_factory=list)
    embedding: Optional[List[float]] = None
    change_summary: Optional[str] = None
    author: str = "system"
    provenance_type: str = "manual"
    provenance_metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def compute_hash(cls, content: str) -> str:

        return hashlib.sha256(content.encode("utf-8")).hexdigest()

class KnowledgeItem(BaseModel):

    id: UUID = Field(default_factory=uuid4)
    workspace_id: str = settings.gateway.default_workspace_id
    is_global: bool = False
    version: int = 1
    title: str
    content: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    is_deleted: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    current_revision: Optional[KnowledgeRevision] = None

    @property
    def current_version(self) -> int:

        return self.version

    @current_version.setter
    def current_version(self, value: int) -> None:
        self.version = value

class DocumentFile(BaseModel):

    id: UUID = Field(default_factory=uuid4)
    workspace_id: str = settings.gateway.default_workspace_id
    is_global: bool = False
    filename: str
    file_path: str
    file_size_bytes: int = 0
    file_size: Optional[int] = None
    content_hash: str = ""
    mime_type: str = "text/plain"
    tags: List[str] = Field(default_factory=list)
    total_chunks: int = 0
    is_deleted: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def model_post_init(self, __context: Any) -> None:
        if self.file_size is None and self.file_size_bytes is not None:
            self.file_size = self.file_size_bytes
        elif self.file_size is not None and (self.file_size_bytes is None or self.file_size_bytes == 0):
            self.file_size_bytes = self.file_size

class DocumentChunk(BaseModel):

    id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    workspace_id: str = settings.gateway.default_workspace_id
    is_global: bool = False
    chunk_index: int = 0
    content: str
    content_hash: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    embedding: Optional[List[float]] = None
    created_at: datetime = Field(default_factory=utc_now)
