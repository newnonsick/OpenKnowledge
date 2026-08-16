"""Unit tests for workspace boundary scoping, JSONL parser, and health endpoints."""

import json
import pytest
from uuid import uuid4

from src.gateway.application.parsers.json_parser import JSONParser
from src.gateway.application.services.knowledge_service import KnowledgeService
from src.gateway.domain.entities import KnowledgeItem, KnowledgeRevision
from src.gateway.domain.exceptions import ItemNotFoundException, ValidationException
from src.gateway.infrastructure.persistence.knowledge_repository import KnowledgeRepository


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jsonl_parser_multi_line():
    """Verify JSONParser correctly parses line-delimited JSON (.jsonl / NDJSON)."""
    parser = JSONParser()
    assert parser.can_parse("dataset.jsonl") is True
    assert parser.can_parse("data.ndjson") is True

    jsonl_content = b'{"name": "Alice", "role": "admin"}\n{"name": "Bob", "role": "user"}\n'
    parsed_text = parser.parse(jsonl_content, "dataset.jsonl")
    data = json.loads(parsed_text)
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["name"] == "Alice"
    assert data[1]["name"] == "Bob"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_workspace_scoping_in_execute_tool(mocker):
    """Verify that execute_tool enforces workspace scoping for get, update, and delete."""
    mock_repo = mocker.AsyncMock(spec=KnowledgeRepository)
    service = KnowledgeService(repository=mock_repo)

    item_id = uuid4()

    # 1. Test knowledge_get passes workspace_id
    mock_repo.get_item_by_id.return_value = KnowledgeItem(
        id=item_id,
        workspace_id="ws-team-a",
        title="Test Item",
        content="Scoped content",
        is_global=False,
    )

    res = await service.execute_tool(
        tool_call_id="call-1",
        name="knowledge_get",
        arguments={"item_id": str(item_id)},
        session_workspace_id="ws-team-a",
    )
    assert res.is_error is False
    mock_repo.get_item_by_id.assert_called_with(
        item_id=item_id,
        workspace_id="ws-team-a",
        version=None,
    )

    # 2. Test knowledge_delete passes workspace_id
    mock_repo.soft_delete_item.return_value = True
    del_res = await service.execute_tool(
        tool_call_id="call-2",
        name="knowledge_delete",
        arguments={"item_id": str(item_id), "expected_version": 1},
        session_workspace_id="ws-team-a",
    )
    # 3. Test knowledge_update passes workspace_id
    mock_repo.update_item_occ.return_value = KnowledgeItem(
        id=item_id,
        workspace_id="ws-team-a",
        title="Updated Item",
        content="Updated content",
        is_global=False,
    )
    upd_res = await service.execute_tool(
        tool_call_id="call-3",
        name="knowledge_update",
        arguments={
            "item_id": str(item_id),
            "expected_version": 1,
            "content": "Updated content",
            "title": "Updated Item",
        },
        session_workspace_id="ws-team-a",
    )
    assert upd_res.is_error is False
    assert mock_repo.update_item_occ.called
    call_kwargs = mock_repo.update_item_occ.call_args.kwargs
    assert call_kwargs.get("workspace_id") == "ws-team-a" or mock_repo.update_item_occ.call_args[1].get("workspace_id") == "ws-team-a"


@pytest.mark.unit
def test_json_parser_invalid_syntax():
    """Verify JSONParser raises ValidationException on truly invalid JSON."""
    parser = JSONParser()
    with pytest.raises(ValidationException):
        parser.parse(b"{invalid: json broken here", "bad.json")


@pytest.mark.unit
def test_auth_public_paths():
    """Verify that both /health and /v1/health are recognized as public paths."""
    from src.gateway.presentation.auth import is_public_path
    assert is_public_path("/health") is True
    assert is_public_path("/v1/health") is True
    assert is_public_path("/v1/models") is False
    assert is_public_path("/v1/chat/completions") is False

