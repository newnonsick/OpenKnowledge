"""Unit tests for Knowledge Subsystem, Versioning, OCC, and KnowledgeService."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4
import pytest

from src.gateway.application.ports.repositories import IKnowledgeRepository
from src.gateway.application.services.knowledge_service import KnowledgeService
from src.gateway.domain.canonical import RankedSearchResult
from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision
from src.gateway.domain.exceptions import ConcurrencyConflictException, ItemNotFoundException, ValidationException


class MockKnowledgeRepository(IKnowledgeRepository):
    """In-memory mock repository for isolated unit testing of KnowledgeService and OCC."""

    def __init__(self) -> None:
        self.items: dict[UUID, KnowledgeItem] = {}
        self.revisions: dict[UUID, list[KnowledgeRevision]] = {}

    async def create_item(self, item: KnowledgeItem, initial_revision: KnowledgeRevision) -> KnowledgeItem:
        self.items[item.id] = item
        self.revisions[item.id] = [initial_revision]
        return item

    async def get_item_by_id(
        self,
        item_id: UUID,
        version: int | None = None,
        workspace_id: str | None = None,
    ) -> KnowledgeItem | None:
        if item_id not in self.items:
            return None
        item = self.items[item_id]
        if item.is_deleted:
            return None
        if workspace_id is not None and not (item.workspace_id == workspace_id or item.is_global):
            return None

        if version is not None:
            revs = [r for r in self.revisions.get(item_id, []) if r.version == version]
            if not revs:
                return None
            item_copy = item.model_copy(deep=True)
            item_copy.current_revision = revs[0]
            item_copy.version = version
            return item_copy

        return item

    async def update_item_occ(
        self,
        item_id: UUID,
        expected_version: int,
        new_revision: KnowledgeRevision,
        title: str | None = None,
        tags: list[str] | None = None,
        is_global: bool | None = None,
        workspace_id: str | None = None,
    ) -> KnowledgeItem:
        if item_id not in self.items or self.items[item_id].is_deleted:
            raise ItemNotFoundException(f"Item '{item_id}' not found.")
        item = self.items[item_id]
        if workspace_id is not None and not (item.workspace_id == workspace_id or item.is_global):
            raise ItemNotFoundException(f"Item '{item_id}' not found in workspace '{workspace_id}'.")
        if item.version != expected_version:
            raise ConcurrencyConflictException(
                message_or_item_id=str(item_id),
                expected_version=expected_version,
                actual_version=item.version,
            )

        new_version = item.version + 1
        new_revision.version = new_version
        item.version = new_version
        item.content = new_revision.content
        item.current_revision = new_revision
        if title is not None:
            item.title = title
        if tags is not None:
            item.tags = tags
        if is_global is not None:
            item.is_global = is_global

        self.revisions[item_id].append(new_revision)
        return item

    async def soft_delete_item(
        self,
        item_id: UUID,
        expected_version: int | None = None,
        workspace_id: str | None = None,
    ) -> bool:
        if item_id not in self.items:
            return False
        item = self.items[item_id]
        if expected_version is not None and item.version != expected_version:
            raise ConcurrencyConflictException(
                message_or_item_id=str(item_id),
                expected_version=expected_version,
                actual_version=item.version,
            )
        item.is_deleted = True
        return True

    async def list_revisions(self, item_id: UUID) -> list[KnowledgeRevision]:
        return list(self.revisions.get(item_id, []))

    async def search_fts(
        self,
        query: str,
        workspace_id: str,
        limit: int = 20,
    ) -> list[RankedSearchResult]:
        results: list[RankedSearchResult] = []
        for rank, item in enumerate(self.items.values(), start=1):
            if not item.is_deleted and (item.workspace_id == workspace_id or item.is_global):
                if query.lower() in item.title.lower() or query.lower() in (item.content or "").lower():
                    results.append(
                        RankedSearchResult(
                            id=str(item.id),
                            source_type="knowledge",
                            title=item.title,
                            content=item.content or "",
                            rank=rank,
                            raw_score=1.0,
                            workspace_id=item.workspace_id,
                            is_global=item.is_global,
                            version=item.version,
                        )
                    )
        return results[:limit]

    async def search_vector(
        self,
        query_vector: list[float],
        workspace_id: str,
        limit: int = 20,
    ) -> list[RankedSearchResult]:
        return []


# ==============================================================================
# Domain Model & Hash Tests
# ==============================================================================

def test_knowledge_revision_compute_hash():
    """Verify SHA-256 hash computation for knowledge content."""
    text = "Core architecture principles."
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert KnowledgeRevision.compute_hash(text) == expected
    assert len(KnowledgeRevision.compute_hash(text)) == 64


def test_knowledge_item_creation_defaults():
    """Verify default initial state of KnowledgeItem."""
    item = KnowledgeItem(title="Guide", content="Some text")
    assert item.version == 1
    assert item.current_version == 1
    assert item.is_deleted is False
    assert item.is_global is False
    assert item.workspace_id == "global"


# ==============================================================================
# KnowledgeService CRUD & OCC Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_knowledge_service_save_item():
    """Verify saving a new knowledge item creates initial revision (v1)."""
    repo = MockKnowledgeRepository()
    service = KnowledgeService(repository=repo)

    item = await service.save_item(
        title="Coding Standards",
        content="Follow clean code and SOLID.",
        workspace_id="ws-dev",
        is_global=False,
        author="alice",
        tags=["standards", "python"],
    )

    assert item.version == 1
    assert item.title == "Coding Standards"
    assert item.workspace_id == "ws-dev"
    assert item.current_revision is not None
    assert item.current_revision.version == 1
    assert item.current_revision.author == "alice"
    assert "standards" in item.tags


@pytest.mark.asyncio
async def test_knowledge_service_save_empty_title_raises_validation():
    """Verify validation on empty title."""
    repo = MockKnowledgeRepository()
    service = KnowledgeService(repository=repo)

    with pytest.raises(ValidationException):
        await service.save_item(title="", content="Body")


@pytest.mark.asyncio
async def test_knowledge_service_update_occ_success():
    """Verify updating with matching expected_version advances to v2."""
    repo = MockKnowledgeRepository()
    service = KnowledgeService(repository=repo)

    item = await service.save_item(
        title="Architecture",
        content="Draft 1",
        workspace_id="global",
    )
    assert item.version == 1

    updated = await service.update_item(
        item_id=item.id,
        content="Draft 2 with improvements",
        expected_version=1,
        author="bob",
        change_summary="Refined architecture",
    )

    assert updated.version == 2
    assert updated.content == "Draft 2 with improvements"
    assert updated.current_revision.version == 2
    assert updated.current_revision.change_summary == "Refined architecture"

    # Revision history has 2 entries
    revs = await service.list_revisions(item.id)
    assert len(revs) == 2
    assert revs[0].version == 1
    assert revs[1].version == 2


@pytest.mark.asyncio
async def test_knowledge_service_update_occ_conflict_raises_409():
    """Verify stale expected_version raises ConcurrencyConflictException."""
    repo = MockKnowledgeRepository()
    service = KnowledgeService(repository=repo)

    item = await service.save_item(title="Docs", content="v1")
    # Advance to v2
    await service.update_item(item_id=item.id, content="v2", expected_version=1)

    # Attempt update with stale expected_version=1
    with pytest.raises(ConcurrencyConflictException) as exc_info:
        await service.update_item(
            item_id=item.id,
            content="Conflicting v2",
            expected_version=1,
        )

    exc = exc_info.value
    assert exc.status_code == 409
    assert exc.details["expected_version"] == 1
    assert exc.details["actual_version"] == 2


@pytest.mark.asyncio
async def test_knowledge_service_soft_delete_and_retrieval():
    """Verify soft deletion hides item from standard retrieval while preserving history."""
    repo = MockKnowledgeRepository()
    service = KnowledgeService(repository=repo)

    item = await service.save_item(title="Temporary Notes", content="To be deleted")
    item_id = item.id

    assert await service.get_item(item_id) is not None

    deleted = await service.delete_item(item_id=item_id)
    assert deleted is True

    # Standard get returns None
    assert await service.get_item(item_id) is None

    # Revisions are still preserved
    revs = await service.list_revisions(item_id)
    assert len(revs) == 1


@pytest.mark.asyncio
async def test_knowledge_service_workspace_scoping_isolation():
    """Verify workspace scoping isolation and global inheritance."""
    repo = MockKnowledgeRepository()
    service = KnowledgeService(repository=repo)

    global_item = await service.save_item("Global Info", "Global content", workspace_id="global", is_global=True)
    ws1_item = await service.save_item("WS1 Info", "WS1 content", workspace_id="ws1", is_global=False)
    ws2_item = await service.save_item("WS2 Info", "WS2 content", workspace_id="ws2", is_global=False)

    # In workspace ws1: global_item and ws1_item should be visible
    assert await service.get_item(global_item.id, workspace_id="ws1") is not None
    assert await service.get_item(ws1_item.id, workspace_id="ws1") is not None
    assert await service.get_item(ws2_item.id, workspace_id="ws1") is None


# ==============================================================================
# Internal Knowledge Tool Execution Tests
# ==============================================================================

@pytest.mark.asyncio
async def test_knowledge_tool_save_and_get():
    """Verify executing knowledge_save and knowledge_get tools."""
    repo = MockKnowledgeRepository()
    service = KnowledgeService(repository=repo)

    # 1. Execute knowledge_save
    save_result = await service.execute_tool(
        tool_call_id="call_save_1",
        name="knowledge_save",
        arguments={"title": "Tool Created Doc", "content": "Tool created body", "workspace_id": "test_ws"},
    )
    assert save_result.is_error is False
    saved_data = json.loads(save_result.content)
    assert saved_data["title"] == "Tool Created Doc"
    assert saved_data["version"] == 1
    item_id = saved_data["id"]

    # 2. Execute knowledge_get
    get_result = await service.execute_tool(
        tool_call_id="call_get_1",
        name="knowledge_get",
        arguments={"item_id": item_id},
    )
    assert get_result.is_error is False
    get_data = json.loads(get_result.content)
    assert get_data["id"] == item_id
    assert get_data["content"] == "Tool created body"


@pytest.mark.asyncio
async def test_knowledge_tool_update_occ_conflict():
    """Verify knowledge_update tool returns error payload on OCC conflict."""
    repo = MockKnowledgeRepository()
    service = KnowledgeService(repository=repo)

    item = await service.save_item(title="Doc", content="v1")

    # Pass wrong expected_version
    update_result = await service.execute_tool(
        tool_call_id="call_up_1",
        name="knowledge_update",
        arguments={"item_id": str(item.id), "expected_version": 99, "content": "Bad update"},
    )
    assert update_result.is_error is True
    err_data = json.loads(update_result.content)
    assert err_data["code"] == "version_mismatch"
    assert err_data["type"] == "concurrency_conflict_error"


@pytest.mark.asyncio
async def test_knowledge_tool_delete():
    """Verify executing knowledge_delete tool."""
    repo = MockKnowledgeRepository()
    service = KnowledgeService(repository=repo)

    item = await service.save_item(title="Doc To Delete", content="bye")
    del_result = await service.execute_tool(
        tool_call_id="call_del_1",
        name="knowledge_delete",
        arguments={"item_id": str(item.id)},
    )
    assert del_result.is_error is False
    del_data = json.loads(del_result.content)
    assert del_data["deleted"] is True
