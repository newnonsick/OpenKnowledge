"""Integration tests for File Upload API endpoint."""

from __future__ import annotations

import io
import pytest
import httpx

from tests.e2e.harness.test_env import TestEnvironment


@pytest.mark.integration
@pytest.mark.asyncio
async def test_file_upload_api_success():
    """Verify POST /v1/files/upload successfully uploads and ingests a file."""
    async with TestEnvironment() as env:
        client = env.get_client(api_key="sk-test-admin")
        async with client:
            file_content = b"# Architecture Overview\n\nPragmatic Clean Architecture in Python."
            files = {
                "file": ("architecture.md", io.BytesIO(file_content), "text/markdown"),
            }
            data = {
                "workspace_id": "ws-backend",
                "is_global": "false",
                "tags": '["architecture", "backend"]',
            }

            resp = await client.post("/v1/files/upload", files=files, data=data)
            assert resp.status_code == 201
            payload = resp.json()
            assert payload["filename"] == "architecture.md"
            assert payload["file_size"] == len(file_content)
            assert payload["workspace_id"] == "ws-backend"
            assert payload["is_global"] is False
            assert payload["total_chunks"] >= 1
            assert "id" in payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_file_upload_api_unauthorized():
    """Verify POST /v1/files/upload returns 401 when unauthenticated."""
    async with TestEnvironment() as env:
        # Client without valid key
        client = env.get_client(api_key=None)
        async with client:
            files = {
                "file": ("test.txt", io.BytesIO(b"content"), "text/plain"),
            }
            resp = await client.post("/v1/files/upload", files=files)
            assert resp.status_code == 401
