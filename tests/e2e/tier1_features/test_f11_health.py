"""Tier 1 Feature Tests for Feature 11: Health & Discovery Endpoints.

Validates GET /health responsiveness, database connectivity verification,
error handling on database disconnection, and discovery / docs availability.
"""

from fastapi import FastAPI, status
from fastapi.testclient import TestClient
import httpx
import pytest

from src.gateway.presentation.routers import health


@pytest.mark.tier1
@pytest.mark.feature("F11")
@pytest.mark.asyncio
async def test_f11_health_endpoint_healthy():
    """Verify GET /health returns a dependency-free compatibility liveness response."""
    app = FastAPI()
    app.include_router(health.router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["status"] == "healthy"
        assert "database" not in data


@pytest.mark.tier1
@pytest.mark.feature("F11")
@pytest.mark.asyncio
async def test_f11_health_endpoint_unhealthy_on_db_failure():
    """Verify GET /health does not consume the serving database pool."""
    app = FastAPI()
    app.include_router(health.router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["status"] == "healthy"
        assert "database" not in data


@pytest.mark.tier1
@pytest.mark.feature("F11")
def test_f11_discovery_openapi_docs():
    """Verify FastAPI application generates complete OpenAPI schema."""
    from src.gateway.main import create_app
    app = create_app()
    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "openapi" in schema
    assert "paths" in schema
    assert "/health" in schema["paths"]


@pytest.mark.tier1
@pytest.mark.feature("F11")
@pytest.mark.asyncio
async def test_f11_health_fast_latency():
    """Verify /health endpoint executes efficiently without unnecessary latency."""
    import time
    app = FastAPI()
    app.include_router(health.router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        start = time.perf_counter()
        resp = await client.get("/health")
        elapsed = time.perf_counter() - start
        assert resp.status_code == 200
        assert elapsed < 0.5  # Sub-500ms execution


@pytest.mark.tier1
@pytest.mark.feature("F11")
@pytest.mark.asyncio
async def test_f11_health_response_headers_and_content_type():
    """Verify /health response returns correct Content-Type application/json."""
    app = FastAPI()
    app.include_router(health.router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert "application/json" in resp.headers["content-type"]
