

from __future__ import annotations

import hashlib
import logging
from typing import List, Optional
from uuid import uuid4

from src.gateway.application.parsers import Chunker, get_parser_for_file
from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.application.ports.repositories import IDocumentRepository
from src.gateway.application.ports.storage import IFileStorage
from src.gateway.config import get_settings
from src.gateway.domain.entities import DocumentChunk, DocumentFile

logger = logging.getLogger(__name__)

class IngestionService:

    def __init__(
        self,
        storage: IFileStorage,
        document_repository: IDocumentRepository,
        embedding_client: Optional[IEmbeddingClient] = None,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
    ) -> None:
        self.storage = storage
        self.document_repository = document_repository
        self.embedding_client = embedding_client
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.chunker = Chunker(default_chunk_size=chunk_size, default_overlap=chunk_overlap)

    async def ingest_file(
        self,
        workspace_id: str,
        filename: str,
        content: bytes,
        mime_type: Optional[str] = None,
        tags: Optional[List[str]] = None,
        is_global: bool = False,
    ) -> DocumentFile:

        file_id = uuid4()
        content_hash = hashlib.sha256(content).hexdigest()
        detected_mime = mime_type or "text/plain"

        effective_is_global = is_global or (
            workspace_id == get_settings().gateway.default_workspace_id
        )

        saved_path = await self.storage.save_file(
            workspace_id=workspace_id,
            file_id=file_id,
            filename=filename,
            content=content,
        )

        parser = get_parser_for_file(filename=filename, mime_type=detected_mime)
        text_content = parser.parse(content, filename=filename)

        chunks_text: List[str] = []
        if text_content:
            chunks_text = self.chunker.chunk_semantic(
                text_content,
                chunk_size=self.chunk_size,
                overlap=self.chunk_overlap,
            )
            if not chunks_text and text_content.strip():
                chunks_text = [text_content]

        embeddings: List[Optional[List[float]]] = [None] * len(chunks_text)
        if chunks_text and self.embedding_client is not None:
            try:
                raw_vectors = await self.embedding_client.embed_texts(chunks_text)
                embeddings = [list(v) for v in raw_vectors]
            except Exception as exc:
                logger.warning(
                    "Ingestion embedding generation failed",
                    extra={"exception_class": type(exc).__name__},
                )

        doc_chunks: List[DocumentChunk] = []
        for idx, chunk_text in enumerate(chunks_text):
            chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            emb = embeddings[idx] if idx < len(embeddings) else None
            doc_chunks.append(
                DocumentChunk(
                    id=uuid4(),
                    document_id=file_id,
                    workspace_id=workspace_id,
                    is_global=effective_is_global,
                    chunk_index=idx,
                    content=chunk_text,
                    content_hash=chunk_hash,
                    metadata={
                        "filename": filename,
                        "mime_type": detected_mime,
                        "tags": tags or [],
                        "content_hash": chunk_hash,
                    },
                    embedding=emb,
                )
            )

        doc_file = DocumentFile(
            id=file_id,
            workspace_id=workspace_id,
            is_global=effective_is_global,
            filename=filename,
            file_path=str(saved_path),
            file_size_bytes=len(content),
            file_size=len(content),
            content_hash=content_hash,
            mime_type=detected_mime,
            tags=tags or [],
            total_chunks=len(doc_chunks),
            is_deleted=False,
        )

        await self.document_repository.save_document(doc_file)
        if doc_chunks:
            await self.document_repository.save_chunks_batch(doc_chunks)

        return doc_file
