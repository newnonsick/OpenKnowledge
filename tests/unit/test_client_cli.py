from __future__ import annotations

import json
from argparse import Namespace

import httpx
import pytest
import respx

from src.gateway.client import (
    execute_archive,
    execute_context,
    execute_create,
    execute_fetch,
    execute_job,
    execute_quota,
    execute_resolve,
    execute_search,
    execute_update,
    execute_upload,
)


def _args(**overrides) -> Namespace:
    base = {"base_url": "https://gateway.test", "api_key": "test-key"}
    base.update(overrides)
    return Namespace(**base)


def test_search_posts_idempotent_query_and_prints_payload(capsys) -> None:
    with respx.mock(base_url="https://gateway.test") as mock:
        route = mock.post("/api/v1/retrieval/search").mock(
            return_value=httpx.Response(200, json={"hits": [], "query": "valve"})
        )
        assert execute_search(_args(query="valve", space=None, limit=10)) == 0
    assert route.called
    assert json.loads(route.calls[0].request.content)["query"] == "valve"
    assert route.calls[0].request.headers["Idempotency-Key"]
    assert json.loads(capsys.readouterr().out)["query"] == "valve"


def test_create_requires_201_and_reports_failure(capsys) -> None:
    with respx.mock(base_url="https://gateway.test") as mock:
        mock.post("/api/v1/knowledge").mock(return_value=httpx.Response(403, text="denied"))
        assert execute_create(_args(space="s", title="t", content="c", tag=None, idempotency_key=None)) == 1
    assert "denied" in capsys.readouterr().out


def test_update_puts_expected_version(capsys) -> None:
    with respx.mock(base_url="https://gateway.test") as mock:
        route = mock.put("/api/v1/knowledge/item-1").mock(
            return_value=httpx.Response(200, json={"id": "item-1"})
        )
        assert (
            execute_update(
                _args(item_id="item-1", expected_version=2, title="t", content="c", tag=["a"], idempotency_key="k")
            )
            == 0
        )
    assert json.loads(route.calls[0].request.content)["expected_version"] == 2
    assert route.calls[0].request.headers["Idempotency-Key"] == "k"


def test_upload_rejects_missing_file(capsys) -> None:
    assert execute_upload(_args(space="s", file="/nonexistent.bin", display_name=None, idempotency_key=None)) == 1
    assert "Cannot read file" in capsys.readouterr().out


def test_job_watch_returns_terminal_state(capsys) -> None:
    payload = {"items": [{"id": "job-1", "state": "succeeded"}]}
    with respx.mock(base_url="https://gateway.test") as mock:
        mock.get("/api/v1/ingestion-jobs").mock(return_value=httpx.Response(200, json=payload))
        assert execute_job(_args(job_id="job-1", space=None, watch=True, timeout=1.0, interval=0.01)) == 0
    assert "succeeded" in capsys.readouterr().out


def test_context_prints_citations_and_excerpts(capsys) -> None:
    payload = {
        "snippets": [
            {
                "title": "Valve",
                "citation_uri": "openknowledge://spaces/global/knowledge/1",
                "snippet": "Close it.",
            }
        ]
    }
    with respx.mock(base_url="https://gateway.test") as mock:
        route = mock.post("/api/v1/context/assemble").mock(return_value=httpx.Response(200, json=payload))
        assert execute_context(_args(query="valve", space=None, limit=5)) == 0
    assert route.called
    body = json.loads(route.calls[0].request.content)
    assert body["query"] == "valve"
    assert body["max_sources"] == 5
    assert "limit" not in body
    out = capsys.readouterr().out
    assert "openknowledge://spaces/global/knowledge/1" in out
    assert "Close it." in out


def test_fetch_gets_exact_revision(capsys) -> None:
    payload = {"canonical_id": "item-1", "content": "exact"}
    with respx.mock(base_url="https://gateway.test") as mock:
        route = mock.get("/api/v1/evidence/knowledge/item-1/revisions/rev-1").mock(
            return_value=httpx.Response(200, json=payload)
        )
        assert execute_fetch(_args(item_id="item-1", revision_id="rev-1")) == 0
    assert route.called
    assert json.loads(capsys.readouterr().out)["content"] == "exact"


def test_resolve_posts_citation_uri(capsys) -> None:
    payload = {"canonical_id": "item-1", "content": "exact"}
    with respx.mock(base_url="https://gateway.test") as mock:
        route = mock.post("/api/v1/evidence/resolve").mock(
            return_value=httpx.Response(200, json=payload)
        )
        assert execute_resolve(_args(citation_uri="openknowledge://spaces/s/knowledge/1/revisions/2")) == 0
    assert json.loads(route.calls[0].request.content)["citation_uri"].startswith("openknowledge://")
    assert json.loads(capsys.readouterr().out)["content"] == "exact"


def test_archive_deletes_with_expected_version(capsys) -> None:
    with respx.mock(base_url="https://gateway.test") as mock:
        route = mock.delete("/api/v1/knowledge/item-1").mock(return_value=httpx.Response(204))
        assert execute_archive(_args(item_id="item-1", expected_version=3, idempotency_key="k")) == 0
    assert route.calls[0].request.url.params["expected_version"] == "3"
    assert route.calls[0].request.headers["Idempotency-Key"] == "k"
    assert json.loads(capsys.readouterr().out)["archived"] is True


def test_quota_shows_usage_and_limits(capsys) -> None:
    payload = {"space_id": "global", "request_count": 3, "requests_limit": 120}
    with respx.mock(base_url="https://gateway.test") as mock:
        route = mock.get("/api/v1/quotas/usage").mock(return_value=httpx.Response(200, json=payload))
        assert execute_quota(_args(space="global")) == 0
    assert route.calls[0].request.url.params["space_id"] == "global"
    assert json.loads(capsys.readouterr().out)["requests_limit"] == 120


def test_missing_api_key_exits() -> None:
    with pytest.raises(SystemExit):
        execute_search(Namespace(base_url="https://gateway.test", api_key=None, query="q", space=None, limit=1))
