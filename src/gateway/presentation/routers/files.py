

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from src.gateway.application.services.ingestion_service import IngestionService
from src.gateway.config import get_settings
from src.gateway.domain.exceptions import GatewayException
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.persistence.document_repository import DocumentRepository
from src.gateway.infrastructure.storage.local_storage import LocalStorageAdapter

router = APIRouter(prefix="/v1/files", tags=["files"])

class FileUploadResponse(BaseModel):

    id: str
    filename: str
    file_size: int
    total_chunks: int
    workspace_id: str
    is_global: bool
    mime_type: str
    created_at: str

def get_ingestion_service() -> IngestionService:

    current_settings = get_settings()
    storage = LocalStorageAdapter(base_dir=current_settings.gateway.storage_dir)
    doc_repo = DocumentRepository()
    emb_client = HTTPEmbeddingClient()
    return IngestionService(
        storage=storage,
        document_repository=doc_repo,
        embedding_client=emb_client,
    )

@router.post("/upload", response_model=FileUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    workspace_id: Optional[str] = Form(None),
    is_global: bool = Form(False),
    tags: Optional[str] = Form(None),
    ingestion_service: IngestionService = Depends(get_ingestion_service),
) -> FileUploadResponse:

    try:
        content = await file.read()
        ws_id = workspace_id or get_settings().gateway.default_workspace_id

        tag_list: List[str] = []
        if tags:
            try:
                parsed_json = json.loads(tags)
                if isinstance(parsed_json, list):
                    tag_list = [str(t).strip() for t in parsed_json if str(t).strip()]
                else:
                    tag_list = [str(tags).strip()]
            except Exception:
                tag_list = [t.strip() for t in tags.split(",") if t.strip()]

        doc_file = await ingestion_service.ingest_file(
            workspace_id=ws_id,
            filename=file.filename or "uploaded_file",
            content=content,
            mime_type=file.content_type or "text/plain",
            tags=tag_list,
            is_global=is_global,
        )

        return FileUploadResponse(
            id=str(doc_file.id),
            filename=doc_file.filename,
            file_size=doc_file.file_size if doc_file.file_size is not None else doc_file.file_size_bytes,
            total_chunks=doc_file.total_chunks,
            workspace_id=doc_file.workspace_id,
            is_global=doc_file.is_global,
            mime_type=doc_file.mime_type,
            created_at=doc_file.created_at.isoformat(),
        )

    except GatewayException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "File ingestion failed.", "type": "server_error"},
        ) from exc
