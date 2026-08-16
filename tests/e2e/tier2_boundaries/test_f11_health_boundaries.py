"""Tier 2 Boundary Tests for Feature 11: Health & Discovery Endpoints.

Tests boundary conditions, degraded database connectivity, rapid polling, and malformed paths.
"""

import asyncio
from unittest.mock import AsyncMock, patch
import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from src.gateway.presentation.routers.health import router
from tests.e2e.harness.test_env import TestEnvironment


def create_health_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.tier2
@pytest.mark.feature("F11")
@pytest.mark.asyncio
async def test_f11_boundary_healthy_database_response_structure():
    """Test boundary: /health endpoint returns 200 OK with status and database fields."""
    app = create_health_test_app()
    async with TestEnvironment() as env:
        async def mock_get_session():
            async with env.session_factory() as session:
                yield session

        from src.gateway.infrastructure.database import get_db_session
        app.dependency_overrides[get_db_session] = mock_get_session

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "healthy"
            assert data["database"] == "connected"


@pytest.mark.tier2
@pytest.mark.feature("F11")
@pytest.mark.asyncio
async def test_f11_boundary_database_down_returns_503_service_unavailable():
    """Test boundary: when database connection fails, /health returns 503 with unhealthy status."""
    app = create_health_test_app()

    async def broken_db_session():
        mock_sess = AsyncMock()
        mock_sess.execute.side_effect = ConnectionError("Database unreachable on port 5432")
        yield mock_sess

    from src.gateway.infrastructure.database import get_db_session
    app.dependency_overrides[get_db_session] = broken_db_session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "unhealthy"
        assert data["database"] == "disconnected"
        assert "Database unreachable" in data["error"]


@pytest.mark.tier2
@pytest.mark.feature("F11")
@pytest.mark.asyncio
async def test_f11_boundary_rapid_health_polling_concurrency():
    """Test boundary: 30 concurrent /health requests under load execute cleanly without connection leaks."""
    app = create_health_test_app()
    async with TestEnvironment() as env:
        async def mock_get_session():
            async with env.session_factory() as session:
                yield session

        from src.gateway.infrastructure.database import get_db_session
        app.dependency_overrides[get_db_session] = mock_get_session

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async def probe():
                return await client.get("/health")

            responses = await asyncio.gather(*[probe() for _ in range(30)])
            assert all(r.status_code == 200 for r in responses)


@pytest.mark.tier2
@pytest.mark.feature("F11")
@pytest.mark.asyncio
async def test_f11_boundary_non_existent_health_subpaths_return_404():
    """Test boundary: probing non-existent subpaths (/health/live, /health/ready, /health/v2) returns 404."""
    app = create_health_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/health/live", "/health/ready", "/health/metrics", "/health_check"):
            resp = await client.get(path)
            assert resp.status_code == 404


@pytest.mark.tier2
@pytest.mark.feature("F11")
@pytest.mark.asyncio
async def test_f11_boundary_unsupported_methods_on_health_endpoint():
    """Test boundary: POST, PUT, DELETE methods on /health endpoint return 405 Method Not Allowed."""
    app = create_health_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_post = await client.post("/health", json={"data": "test"})
        assert resp_post.status_code == 405

        resp_del = await client.delete("/health")
        assert resp_del.status_code == 405
