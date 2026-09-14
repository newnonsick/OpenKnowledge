from __future__ import annotations

import httpx
import pytest
import respx

from src.gateway import OpenKnowledgeClient, OpenKnowledgeError
from src.gateway.client import DEFAULT_BASE_URL


BASE_URL = "https://gateway.test"


def _client(**overrides) -> OpenKnowledgeClient:
    params = {"base_url": BASE_URL, "api_key": "fresh-key"}
    params.update(overrides)
    return OpenKnowledgeClient(**params)


def test_sdk_first_search_write_flow_for_fresh_user() -> None:
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/api/v1/retrieval/search").mock(
            side_effect=[
                httpx.Response(200, json={"query": "blue valve", "hits": []}),
                httpx.Response(
                    200,
                    json={
                        "query": "blue valve",
                        "hits": [
                            {
                                "canonical_id": "item-1",
                                "revision_id": "rev-1",
                                "title": "Valve",
                            }
                        ],
                    },
                ),
            ]
        )
        create_route = mock.post("/api/v1/knowledge").mock(
            return_value=httpx.Response(201, json={"id": "item-1", "version": 1})
        )
        update_route = mock.put("/api/v1/knowledge/item-1").mock(
            return_value=httpx.Response(200, json={"id": "item-1", "version": 2})
        )
        fetch_route = mock.get("/api/v1/evidence/knowledge/item-1/revisions/rev-1").mock(
            return_value=httpx.Response(200, json={"canonical_id": "item-1", "content": "exact"})
        )
        resolve_route = mock.post("/api/v1/evidence/resolve").mock(
            return_value=httpx.Response(200, json={"canonical_id": "item-1", "content": "exact"})
        )
        archive_route = mock.delete("/api/v1/knowledge/item-1").mock(
            return_value=httpx.Response(204)
        )
        with _client() as sdk:
            session = sdk._client
            assert sdk.search("blue valve")["hits"] == []
            created = sdk.create("global", "Valve", "Closes clockwise.", tags=["ops"])
            assert created["id"] == "item-1"
            hits = sdk.search("blue valve")["hits"]
            assert [hit["canonical_id"] for hit in hits] == ["item-1"]
            updated = sdk.update("item-1", 1, "Valve", "Closes clockwise, firmly.")
            assert updated["version"] == 2
            assert sdk.fetch("item-1", "rev-1")["content"] == "exact"
            assert sdk.resolve("openknowledge://spaces/global/knowledge/item-1/revisions/rev-1")["content"] == "exact"
            assert sdk.archive("item-1", 2) == {"id": "item-1", "archived": True}
            assert sdk._client is session
    assert create_route.called
    assert update_route.called
    assert fetch_route.called
    assert resolve_route.called
    assert archive_route.called


def test_sdk_shares_single_session_for_context_quota_and_jobs() -> None:
    payload = {"items": [{"id": "job-1", "state": "succeeded"}]}
    with respx.mock(base_url=BASE_URL) as mock:
        context_route = mock.post("/api/v1/context/assemble").mock(
            return_value=httpx.Response(200, json={"snippets": [], "query": "valve"})
        )
        quota_route = mock.get("/api/v1/quotas/usage").mock(
            return_value=httpx.Response(200, json={"space_id": "global"})
        )
        jobs_route = mock.get("/api/v1/ingestion-jobs").mock(
            return_value=httpx.Response(200, json=payload)
        )
        with _client() as sdk:
            session = sdk._client
            assert sdk.context("valve")["query"] == "valve"
            assert sdk.quota()["space_id"] == "global"
            assert sdk.jobs(job_id="job-1") == payload["items"]
            assert sdk.watch_job("job-1", interval=0.01)["state"] == "succeeded"
            assert sdk._client is session
    assert context_route.called
    assert quota_route.called
    assert jobs_route.called


def test_sdk_uploads_file_and_reports_mapped_errors(tmp_path) -> None:
    target = tmp_path / "runbook.txt"
    target.write_bytes(b"close it")
    with respx.mock(base_url=BASE_URL) as mock:
        upload_route = mock.post("/api/v1/sources/upload").mock(
            return_value=httpx.Response(202, json={"job_id": "job-9"})
        )
        denied = mock.post("/api/v1/knowledge").mock(
            return_value=httpx.Response(403, text="denied")
        )
        with _client() as sdk:
            assert sdk.upload("global", target)["job_id"] == "job-9"
            with pytest.raises(OpenKnowledgeError) as excinfo:
                sdk.create("global", "t", "c")
            assert excinfo.value.status_code == 403
            assert excinfo.value.response_text == "denied"
            with pytest.raises(OpenKnowledgeError):
                sdk.upload("global", tmp_path / "missing.bin")
    assert upload_route.called
    assert denied.called


def test_sdk_reads_env_and_rejects_missing_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENKNOWLEDGE_URL", BASE_URL)
    monkeypatch.setenv("OPENKNOWLEDGE_API_KEY", "env-key")
    with OpenKnowledgeClient() as sdk:
        assert sdk.base_url == BASE_URL
        assert sdk._client.headers["Authorization"] == "Bearer env-key"
    monkeypatch.delenv("OPENKNOWLEDGE_API_KEY")
    with pytest.raises(OpenKnowledgeError):
        OpenKnowledgeClient()
    assert DEFAULT_BASE_URL == "http://127.0.0.1:8000"
