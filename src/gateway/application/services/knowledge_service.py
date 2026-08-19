

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from src.gateway.config import get_settings
from src.gateway.application.ports.clients import IEmbeddingClient
from src.gateway.application.ports.repositories import IKnowledgeRepository
from src.gateway.domain.canonical import RankedSearchResult
from src.gateway.domain.entities import (
    KnowledgeItem,
    KnowledgeRevision,
)
from src.gateway.domain.exceptions import (
    ConcurrencyConflictException,
    ItemNotFoundException,
    ToolExecutionException,
    ValidationException,
)
from src.gateway.domain.tools import ToolResult

logger = logging.getLogger(__name__)

class KnowledgeService:

    def __init__(
        self,
        repository: IKnowledgeRepository,
        embedding_client: Optional[IEmbeddingClient] = None,
    ) -> None:
        self.repository = repository
        self.embedding_client = embedding_client

    async def get_item(
        self,
        item_id: UUID,
        workspace_id: Optional[str] = None,
        version: Optional[int] = None,
    ) -> Optional[KnowledgeItem]:

        return await self.repository.get_item_by_id(
            item_id=item_id,
            version=version,
            workspace_id=workspace_id,
        )

    async def save_item(
        self,
        title: str,
        content: str,
        workspace_id: Optional[str] = None,
        is_global: bool = False,
        author: str = "system",
        tags: Optional[List[str]] = None,
    ) -> KnowledgeItem:

        if not title or not title.strip():
            raise ValidationException("Title cannot be empty.")
        if content is None:
            raise ValidationException("Content cannot be None.")

        ws_id = workspace_id or get_settings().gateway.default_workspace_id
        item_id = uuid4()
        content_hash = KnowledgeRevision.compute_hash(content)

        embedding: Optional[List[float]] = None
        if self.embedding_client is not None:
            try:
                embeddings = await self.embedding_client.embed_texts([f"{title}\n\n{content}"])
                if embeddings:
                    embedding = embeddings[0]
            except Exception as exc:
                logger.warning(
                    "Knowledge embedding generation failed",
                    extra={"exception_class": type(exc).__name__},
                )

        rev_id = uuid4()
        initial_revision = KnowledgeRevision(
            id=rev_id,
            item_id=item_id,
            version=1,
            title=title,
            content=content,
            content_hash=content_hash,
            tags=tags or [],
            embedding=embedding,
            author=author,
        )

        item = KnowledgeItem(
            id=item_id,
            workspace_id=ws_id,
            is_global=is_global or (ws_id == get_settings().gateway.default_workspace_id),
            version=1,
            title=title,
            content=content,
            tags=tags or [],
            is_deleted=False,
            current_revision=initial_revision,
        )

        return await self.repository.create_item(item, initial_revision)

    async def update_item(
        self,
        item_id: UUID,
        content: str,
        expected_version: int,
        workspace_id: Optional[str] = None,
        author: str = "system",
        title: Optional[str] = None,
        tags: Optional[List[str]] = None,
        is_global: Optional[bool] = None,
        change_summary: Optional[str] = None,
    ) -> KnowledgeItem:

        if expected_version < 1:
            raise ConcurrencyConflictException(
                message_or_item_id=str(item_id),
                expected_version=expected_version,
                actual_version=1,
                message=f"Invalid expected_version {expected_version}: must be >= 1.",
            )

        content_hash = KnowledgeRevision.compute_hash(content)

        embedding: Optional[List[float]] = None
        if self.embedding_client is not None:
            try:
                effective_title = title
                if effective_title is None:
                    try:
                        existing = await self.repository.get_item_by_id(item_id=item_id, workspace_id=workspace_id)
                        if existing:
                            effective_title = existing.title
                    except Exception:
                        pass
                text_to_embed = f"{effective_title or ''}\n\n{content}".strip()
                embeddings = await self.embedding_client.embed_texts([text_to_embed])
                if embeddings:
                    embedding = embeddings[0]
            except Exception as exc:
                logger.warning(
                    "Knowledge update embedding generation failed",
                    extra={"exception_class": type(exc).__name__},
                )

        new_revision = KnowledgeRevision(
            id=uuid4(),
            item_id=item_id,
            version=expected_version + 1,
            title=title,
            content=content,
            content_hash=content_hash,
            tags=tags or [],
            embedding=embedding,
            change_summary=change_summary,
            author=author,
        )

        return await self.repository.update_item_occ(
            item_id=item_id,
            expected_version=expected_version,
            new_revision=new_revision,
            title=title,
            tags=tags,
            is_global=is_global,
            workspace_id=workspace_id,
        )

    async def delete_item(
        self,
        item_id: UUID,
        workspace_id: Optional[str] = None,
        expected_version: Optional[int] = None,
    ) -> bool:

        return await self.repository.soft_delete_item(
            item_id=item_id,
            expected_version=expected_version,
            workspace_id=workspace_id,
        )

    async def list_revisions(self, item_id: UUID) -> List[KnowledgeRevision]:

        return await self.repository.list_revisions(item_id)

    async def search_items(
        self,
        query: str,
        workspace_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[RankedSearchResult]:

        ws_id = workspace_id or get_settings().gateway.default_workspace_id
        return await self.repository.search_fts(
            query=query,
            workspace_id=ws_id,
            limit=limit,
        )

    async def execute_tool(
        self,
        tool_call_id: str,
        name: str,
        arguments: Dict[str, Any],
        session_workspace_id: Optional[str] = None,
    ) -> ToolResult:

        ws_id = arguments.get("workspace_id") or session_workspace_id
        try:
            if name == "knowledge_search":
                query = str(arguments.get("query", ""))
                search_ws = ws_id or get_settings().gateway.default_workspace_id
                limit = int(arguments.get("limit", 5))
                results = await self.search_items(query=query, workspace_id=search_ws, limit=limit)
                content_payload = [
                    {
                        "id": r.id,
                        "title": r.title,
                        "content": r.content,
                        "score": r.raw_score,
                        "workspace_id": r.workspace_id,
                        "version": r.version,
                    }
                    for r in results
                ]
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    content=json.dumps({"results": content_payload, "count": len(content_payload)}),
                    is_error=False,
                )

            elif name == "knowledge_get":
                raw_id = arguments.get("item_id")
                if not raw_id:
                    raise ValidationException("Missing 'item_id' parameter.")
                item_uuid = UUID(str(raw_id))
                version = arguments.get("version")
                ver_int = int(version) if version is not None else None
                item = await self.get_item(item_id=item_uuid, workspace_id=ws_id, version=ver_int)
                if item is None:
                    raise ItemNotFoundException(f"Knowledge item '{item_uuid}' not found.")
                payload = {
                    "id": str(item.id),
                    "title": item.title,
                    "content": item.content,
                    "version": item.version,
                    "workspace_id": item.workspace_id,
                    "is_global": item.is_global,
                    "tags": item.tags,
                    "created_at": item.created_at.isoformat(),
                    "updated_at": item.updated_at.isoformat(),
                }
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    content=json.dumps(payload),
                    is_error=False,
                )

            elif name == "knowledge_save":
                title = str(arguments.get("title", ""))
                content = str(arguments.get("content", ""))
                save_ws = ws_id or get_settings().gateway.default_workspace_id
                is_global = bool(arguments.get("is_global", False))
                tags = arguments.get("tags") or []
                item = await self.save_item(
                    title=title,
                    content=content,
                    workspace_id=save_ws,
                    is_global=is_global,
                    tags=tags,
                )
                payload = {
                    "id": str(item.id),
                    "title": item.title,
                    "version": item.version,
                    "workspace_id": item.workspace_id,
                    "is_global": item.is_global,
                    "status": "created",
                }
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    content=json.dumps(payload),
                    is_error=False,
                )

            elif name == "knowledge_update":
                raw_id = arguments.get("item_id")
                if not raw_id:
                    raise ValidationException("Missing 'item_id' parameter.")
                item_uuid = UUID(str(raw_id))
                expected_ver = int(arguments.get("expected_version", 0))
                content = str(arguments.get("content", ""))
                title = arguments.get("title")
                tags = arguments.get("tags")
                is_global = arguments.get("is_global")
                change_summary = arguments.get("change_summary")
                item = await self.update_item(
                    item_id=item_uuid,
                    content=content,
                    expected_version=expected_ver,
                    workspace_id=ws_id,
                    title=title,
                    tags=tags,
                    is_global=is_global,
                    change_summary=change_summary,
                )
                payload = {
                    "id": str(item.id),
                    "title": item.title,
                    "version": item.version,
                    "workspace_id": item.workspace_id,
                    "status": "updated",
                }
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    content=json.dumps(payload),
                    is_error=False,
                )

            elif name == "knowledge_delete":
                raw_id = arguments.get("item_id")
                if not raw_id:
                    raise ValidationException("Missing 'item_id' parameter.")
                item_uuid = UUID(str(raw_id))
                expected_ver = arguments.get("expected_version")
                ver_int = int(expected_ver) if expected_ver is not None else None

                success = await self.delete_item(
                    item_id=item_uuid,
                    workspace_id=ws_id,
                    expected_version=ver_int,
                )
                payload = {
                    "id": str(item_uuid),
                    "deleted": success,
                    "status": "deleted" if success else "not_found",
                }
                return ToolResult(
                    tool_call_id=tool_call_id,
                    name=name,
                    content=json.dumps(payload),
                    is_error=False,
                )

            else:
                raise ToolExecutionException(f"Unknown tool name: '{name}'")

        except (ConcurrencyConflictException, ItemNotFoundException, ValidationException) as exc:
            return ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                content=json.dumps(exc.to_dict()),
                is_error=True,
            )
        except Exception as exc:
            logger.error(
                "Knowledge tool execution failed",
                extra={"tool_name": name, "exception_class": type(exc).__name__},
            )
            return ToolResult(
                tool_call_id=tool_call_id,
                name=name,
                content=json.dumps(
                    {
                        "error": "The tool could not complete the request.",
                        "type": "tool_execution_error",
                    }
                ),
                is_error=True,
            )
