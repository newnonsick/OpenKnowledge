from __future__ import annotations

import secrets
import time
from uuid import uuid4

from tests.e2e.live.conftest import LiveClient, pytestmark  # noqa: F401


def wait_for_job(admin_client: LiveClient, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        response = admin_client.get("/api/v1/ingestion-jobs", params={"limit": 100})
        assert response.status_code == 200, response.text
        for job in response.json()["items"]:
            if job["id"] == job_id:
                last = job
                if job["state"] in {"succeeded", "failed", "cancelled"}:
                    return job
        time.sleep(2)
    return last


def test_upload_and_ingestion_pipeline_activates_document(admin_client, space_factory):
    space = space_factory(f"Ingestion Space {secrets.token_hex(4)}")
    space_id = space["id"]
    marker = secrets.token_hex(8)

    document_text = (
        f"Project Phoenix Operations Manual\n\n"
        f"The recovery phrase for deployment is PHOENIX-{marker}. "
        f"Store this safely. The rollback coordinator is reachable at extension {marker[:4]}."
    )

    upload = admin_client.post(
        "/api/v1/sources/upload",
        files={"file": (f"phoenix-manual-{marker}.txt", document_text.encode("utf-8"), "text/plain")},
        data={"space_id": space_id, "display_name": f"Phoenix Manual {marker}"},
        idempotency_key=f"e2e-up-{uuid4()}",
    )
    assert upload.status_code == 202, upload.text
    receipt = upload.json()
    assert receipt["job_state"] in {"queued", "running"}
    job_id = receipt["job_id"]
    document_id = receipt["document_id"]

    job = wait_for_job(admin_client, job_id)
    assert job["state"] == "succeeded", job

    sources = admin_client.get("/api/v1/sources", params={"space_ids": space_id, "limit": 100})
    assert sources.status_code == 200, sources.text

    listing = admin_client.get("/api/v1/sources", params={"limit": 100})
    assert listing.status_code == 200, listing.text
    target = [s for s in listing.json()["items"] if s["id"] == document_id]
    assert target, "uploaded source must be listed"
    assert target[0]["status"] == "active", target[0]

    search = admin_client.post(
        "/api/v1/retrieval/search",
        json_body={"query": f"What is the recovery phrase for deployment? PHOENIX-{marker}", "space_ids": [space_id], "limit": 10},
    )
    assert search.status_code == 200, search.text
    flat = str(search.json())
    assert marker in flat, "activated document chunks must be retrievable"

    archive = admin_client.delete(
        f"/api/v1/sources/{document_id}",
        params={"expected_revision": target[0]["revision"]},
        idempotency_key=f"e2e-src-del-{secrets.token_hex(8)}",
    )
    assert archive.status_code == 204, archive.text


def test_upload_markdown_and_json_documents(admin_client, space_factory):
    space = space_factory(f"Multi-format Space {secrets.token_hex(4)}")
    space_id = space["id"]

    uploads = []
    md_marker = secrets.token_hex(6)
    uploads.append((
        f"notes-{md_marker}.md",
        f"# Runbook\n\nEmergency contact: OPS-{md_marker}@example.invalid".encode("utf-8"),
        "text/markdown",
    ))
    json_marker = secrets.token_hex(6)
    uploads.append((
        f"config-{json_marker}.json",
        b'{"service": "gateway", "replicas": 3, "note": "jconfig-' + json_marker.encode() + b'"}',
        "application/json",
    ))

    receipts = []
    for filename, content, mime in uploads:
        response = admin_client.post(
            "/api/v1/sources/upload",
            files={"file": (filename, content, mime)},
            data={"space_id": space_id},
            idempotency_key=f"e2e-up-{uuid4()}",
        )
        assert response.status_code == 202, response.text
        receipts.append(response.json())

    for receipt in receipts:
        job = wait_for_job(admin_client, receipt["job_id"])
        assert job["state"] == "succeeded", job


def test_upload_rejects_missing_space(admin_client):
    response = admin_client.post(
        "/api/v1/sources/upload",
        files={"file": ("orphan.txt", b"no space provided", "text/plain")},
        idempotency_key=f"e2e-up-{uuid4()}",
    )
    assert response.status_code == 422, response.text


def test_ingestion_job_mutations_authorize_space(admin_client, member_factory, space_factory):
    outsider = member_factory("lvo")
    space = space_factory(f"Job Authz Space {secrets.token_hex(4)}")

    upload = admin_client.post(
        "/api/v1/sources/upload",
        files={"file": ("job-authz.txt", b"authorization probe", "text/plain")},
        data={"space_id": space["id"]},
        idempotency_key=f"e2e-up-{uuid4()}",
    )
    assert upload.status_code == 202, upload.text
    job_id = upload.json()["job_id"]

    as_outsider = outsider["client"]
    denied_cancel = as_outsider.post(
        f"/api/v1/ingestion-jobs/{job_id}/cancel",
        json_body={},
        idempotency_key=f"e2e-job-cx-{secrets.token_hex(8)}",
    )
    assert denied_cancel.status_code in (403, 404), denied_cancel.text

    cancel = admin_client.post(
        f"/api/v1/ingestion-jobs/{job_id}/cancel",
        json_body={},
        idempotency_key=f"e2e-job-cx-{secrets.token_hex(8)}",
    )
    assert cancel.status_code == 200, cancel.text
