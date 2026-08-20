from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from src.gateway.config import Settings
from src.gateway.infrastructure.database import get_db_session
from src.gateway.main import create_app
from src.gateway.presentation.routers.chat_completions import _handle_streaming_completion
from src.gateway.presentation.routers.messages import _handle_anthropic_streaming
from src.gateway.presentation.security_headers import apply_security_headers
from starlette.responses import Response


def boundary_settings() -> Settings:
    return Settings(
        gateway={
            "environment": "test",
            "api_keys": ["test-key"],
            "legacy_api_keys_enabled": True,
            "cors_origins": ["http://localhost:3000"],
            "trusted_hosts": ["testserver"],
        }
    )


def assert_security_headers(response: httpx.Response) -> None:
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "camera=()" in response.headers["permissions-policy"]
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_management_api_responses_are_never_cacheable() -> None:
    response = apply_security_headers(Response(), "/api/v1/spaces")

    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_liveness_has_request_id_without_database_access():
    app = create_app(boundary_settings())

    async def forbidden_database_dependency():
        raise AssertionError("liveness accessed database")
        yield

    app.dependency_overrides[get_db_session] = forbidden_database_dependency
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz/live")

    assert response.status_code == 200
    assert response.json()["status"] == "live"
    request_id = response.headers["x-request-id"]
    assert response.json()["request_id"] == request_id
    UUID(request_id)
    assert_security_headers(response)


@pytest.mark.asyncio
async def test_valid_request_id_is_echoed_and_malformed_value_is_replaced():
    app = create_app(boundary_settings())
    transport = httpx.ASGITransport(app=app)
    supplied = str(uuid4())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        echoed = await client.get("/healthz/live", headers={"X-Request-ID": supplied})
        replaced = await client.get("/healthz/live", headers={"X-Request-ID": "bad\r\nid"})

    assert echoed.headers["x-request-id"] == supplied
    assert replaced.headers["x-request-id"] != "bad\r\nid"
    UUID(replaced.headers["x-request-id"])


@pytest.mark.asyncio
async def test_readiness_failure_is_bounded_and_does_not_leak_dependency_error():
    sentinel = "postgresql://admin:sentinel-db-secret@private-db/gateway"
    app = create_app(boundary_settings())

    class BrokenProbe:
        async def check(self):
            return False

    async def broken_database_dependency():
        raise AssertionError(sentinel)
        yield

    app.state.readiness_probe = BrokenProbe()
    app.dependency_overrides[get_db_session] = broken_database_dependency
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/healthz/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert sentinel not in response.text
    assert_security_headers(response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected_shape"),
    [
        ("/v1/chat/completions/probe", "openai"),
        ("/v1/messages/probe", "anthropic"),
    ],
)
async def test_unhandled_errors_use_protocol_safe_envelopes(path, expected_shape):
    sentinel = "sentinel-provider-secret-body"
    app = create_app(boundary_settings())

    async def boom():
        raise RuntimeError(sentinel)

    app.add_api_route(path, boom, methods=["GET"])
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(path, headers={"Authorization": "Bearer test-key"})

    payload = response.json()
    assert response.status_code == 500
    assert sentinel not in response.text
    if expected_shape == "anthropic":
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "api_error"
    else:
        assert payload["error"]["type"] == "internal_server_error"
        assert payload["error"]["code"] == "internal_error"
    assert payload["request_id"] == response.headers["x-request-id"]
    assert_security_headers(response)


@pytest.mark.asyncio
async def test_authentication_error_has_request_id_and_security_headers():
    app = create_app(boundary_settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/v1/chat/completions", json={})

    assert response.status_code == 401
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    assert_security_headers(response)


class FailingStreamOrchestrator:
    async def orchestrate_chat_stream(self, request, workspace_id):
        raise RuntimeError("sentinel-stream-secret")
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "protocol"),
    [
        (_handle_streaming_completion, "openai"),
        (_handle_anthropic_streaming, "anthropic"),
    ],
)
async def test_streaming_errors_do_not_leak_exception_text(factory, protocol):
    response = factory(
        canonical_req=SimpleNamespace(model="test-model", workspace_id="global"),
        orchestrator=FailingStreamOrchestrator(),
    )

    parts = [part async for part in response.body_iterator]
    body = "".join(
        part.decode("utf-8") if isinstance(part, bytes) else part
        for part in parts
    )

    assert "sentinel-stream-secret" not in body
    if protocol == "anthropic":
        assert "api_error" in body
    else:
        assert "streaming_error" in body
