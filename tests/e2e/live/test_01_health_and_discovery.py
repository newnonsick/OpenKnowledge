from __future__ import annotations

import os

import httpx

from tests.e2e.live.conftest import BASE_URL, pytestmark  # noqa: F401


def test_public_health_endpoints():
    live = httpx.Client(base_url=BASE_URL, timeout=30.0)
    try:
        for path in ("/health", "/v1/health"):
            response = live.get(path)
            assert response.status_code == 200, (path, response.text)
            body = response.json()
            assert body["status"] == "healthy"
            assert body["request_id"]

        liveness = live.get("/healthz/live")
        assert liveness.status_code == 200, liveness.text
        assert liveness.json()["status"] == "live"

        ready = live.get("/healthz/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["status"] == "ready"
    finally:
        live.close()


def test_openapi_document_is_served():
    live = httpx.Client(base_url=BASE_URL, timeout=30.0)
    try:
        schema = live.get("/openapi.json").json()
        assert schema["info"]["title"] == "OpenKnowledge"
        for required in ("/v1/chat/completions", "/v1/messages", "/api/v1/knowledge"):
            assert required in schema["paths"]
    finally:
        live.close()


def test_metrics_endpoint_exposes_counters():
    live = httpx.Client(base_url=BASE_URL, timeout=30.0)
    try:
        text = live.get("/metrics").text
        assert "gateway_http_requests_total" in text
    finally:
        live.close()


def test_protected_endpoints_reject_unauthenticated_requests():
    live = httpx.Client(base_url=BASE_URL, timeout=30.0)
    try:
        assert live.get("/v1/models").status_code == 401
        chat = live.post(
            "/v1/chat/completions",
            json={"model": "default", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert chat.status_code == 401
        knowledge = live.get("/api/v1/knowledge")
        assert knowledge.status_code == 401
        garbage = live.get("/v1/models", headers={"Authorization": "Bearer not-a-real-key"})
        assert garbage.status_code == 401
    finally:
        live.close()


def test_models_registry_lists_configured_backend_model(api_key: str):
    client = httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )
    try:
        listing = client.get("/v1/models")
        assert listing.status_code == 200, listing.text
        models = listing.json()["data"]
        assert models, "model registry must not be empty"

        default_model = os.environ.get("OPENKNOWLEDGE_LIVE_EXPECTED_MODEL")
        if default_model:
            ids = {m["id"] for m in models}
            assert default_model in ids

        first = models[0]["id"]
        detail = client.get(f"/v1/models/{first}")
        assert detail.status_code == 200, detail.text
        missing = client.get("/v1/models/does-not-exist-xyz")
        assert missing.status_code == 404
    finally:
        client.close()
