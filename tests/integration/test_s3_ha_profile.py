from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import yaml

from src.gateway.config import Settings
from src.gateway.infrastructure.storage.factory import build_versioned_object_storage
from src.gateway.infrastructure.storage.s3_storage import S3CompatibleStorageAdapter


BUCKET = "ha-profile-bucket"
SPACE_ID = "s3-ha-profile"
COMPOSE_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "compose.s3.yaml")


def _listing(keys: list[tuple[str, int]]) -> bytes:
    members = "".join(
        f"<Contents><Key>{key}</Key><Size>{size}</Size>"
        f"<LastModified>2026-09-13T00:00:00Z</LastModified></Contents>"
        for key, size in keys
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"{members}<IsTruncated>false</IsTruncated></ListBucketResult>"
    ).encode()


class StubS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.multipart: dict[str, dict[str, bytes]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        prefix = f"/{BUCKET}/"
        path = request.url.path
        key = path[len(prefix):] if path.startswith(prefix) else ""
        if request.method == "HEAD":
            return httpx.Response(200 if key in self.objects else 404)
        if request.method == "GET" and not key:
            listed = sorted(
                (name, len(body))
                for name, body in self.objects.items()
                if name.startswith(params.get("prefix", ""))
            )
            return httpx.Response(200, content=_listing(listed))
        if request.method == "GET":
            if key in self.objects:
                return httpx.Response(200, content=self.objects[key])
            return httpx.Response(404, content=b"<Error><Code>NoSuchKey</Code></Error>")
        if request.method == "PUT":
            lowered = {name.lower(): value for name, value in request.headers.items()}
            if "uploadId" in params and "partNumber" in params:
                bucket = self.multipart.setdefault(params["uploadId"], {})
                bucket[params["partNumber"]] = request.content
                return httpx.Response(200, headers={"ETag": f'"etag-{params["partNumber"]}"'})
            if "x-amz-copy-source" in lowered:
                source = lowered["x-amz-copy-source"].replace("%2F", "/")
                source_key = source.split(f"/{BUCKET}/", 1)[-1]
                if source_key not in self.objects:
                    return httpx.Response(404, content=b"<Error><Code>NoSuchKey</Code></Error>")
                if key in self.objects:
                    return httpx.Response(200)
                self.objects[key] = self.objects[source_key]
                return httpx.Response(
                    200,
                    content=b"<CopyObjectResult><ETag>etag</ETag></CopyObjectResult>",
                )
            self.objects[key] = request.content
            return httpx.Response(200)
        if request.method == "POST" and "uploads" in params:
            upload_id = f"upload-{len(self.multipart)}"
            self.multipart[upload_id] = {}
            return httpx.Response(
                200,
                content=(
                    "<InitiateMultipartUploadResult>"
                    f"<UploadId>{upload_id}</UploadId>"
                    "</InitiateMultipartUploadResult>"
                ).encode(),
            )
        if request.method == "POST" and "uploadId" in params:
            upload_id = params["uploadId"]
            parts = self.multipart.pop(upload_id, None)
            if parts is None:
                return httpx.Response(404, content=b"<Error><Code>NoSuchUpload</Code></Error>")
            ordered = [parts[number] for number in sorted(parts, key=int)]
            self.objects[key] = b"".join(ordered)
            return httpx.Response(
                200,
                content=b"<CompleteMultipartUploadResult><ETag>final</ETag></CompleteMultipartUploadResult>",
            )
        if request.method == "DELETE":
            if "uploadId" in params:
                self.multipart.pop(params["uploadId"], None)
                return httpx.Response(204)
            if key in self.objects:
                del self.objects[key]
                return httpx.Response(204)
            return httpx.Response(404)
        return httpx.Response(400)


async def _chunks(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def _s3_settings(**overrides: object) -> Settings:
    gateway: dict[str, object] = {
        "storage_backend": "s3",
        "storage_s3_endpoint": "https://s3.example.test",
        "storage_s3_bucket": BUCKET,
        "storage_s3_region": "us-east-1",
        "storage_s3_access_key": "ha-access",
        "storage_s3_secret_key": "ha-secret",
    }
    gateway.update(overrides)
    return Settings(
        llm={"url": "http://localhost:8888", "model_id": "m"},
        embedding={"url": "http://localhost:7997", "model_id": "e", "dimension": 8},
        database={"url": "postgresql+asyncpg://postgres:postgres@localhost:5432/gateway_test"},
        gateway=gateway,
    )


def test_compose_s3_override_pins_s3_backend_for_gateway_and_worker() -> None:
    with open(COMPOSE_PATH, encoding="utf-8") as handle:
        profile = yaml.safe_load(handle)
    for service in ("gateway", "worker"):
        environment = profile["services"][service]["environment"]
        assert environment["STORAGE_BACKEND"] == "s3"
        for variable in (
            "STORAGE_S3_ENDPOINT",
            "STORAGE_S3_BUCKET",
            "STORAGE_S3_REGION",
            "STORAGE_S3_ACCESS_KEY",
            "STORAGE_S3_SECRET_KEY",
        ):
            assert variable in environment


@pytest.mark.asyncio
async def test_s3_backend_export_import_roundtrip_on_stub() -> None:
    stub = StubS3()
    client = httpx.AsyncClient(transport=httpx.MockTransport(stub.handler))
    adapter = S3CompatibleStorageAdapter(
        endpoint="https://s3.example.test",
        bucket=BUCKET,
        region="us-east-1",
        access_key="ha-access",
        secret_key="ha-secret",
        client=client,
    )
    payload = b"s3 ha export payload " + uuid4().hex.encode()
    upload_id = uuid4()
    document_id = uuid4()
    revision_id = uuid4()
    staged = await adapter.stage(
        space_id=SPACE_ID,
        upload_id=upload_id,
        chunks=_chunks(payload),
        max_bytes=len(payload) + 1024,
    )
    assert staged.storage_key == f"staging/{SPACE_ID}/{upload_id}"
    assert staged.checksum_sha256 == hashlib.sha256(payload).hexdigest()
    exported = await adapter.read(staged.storage_key)
    final_key = await adapter.finalize(
        staged.storage_key,
        space_id=SPACE_ID,
        document_id=document_id,
        revision_id=revision_id,
    )
    assert final_key == f"objects/{SPACE_ID}/{document_id}/{revision_id}"
    imported = await adapter.read(final_key)
    assert imported == exported == payload
    keys = {item.storage_key for item in await adapter.list_objects("objects")}
    assert final_key in keys
    assert await adapter.delete(final_key) is True
    assert await adapter.exists(final_key) is False


@pytest.mark.asyncio
async def test_storage_factory_builds_s3_adapter_for_ha_profile() -> None:
    settings = _s3_settings()
    settings.gateway.validate_storage_backend()
    storage = build_versioned_object_storage(settings)
    assert isinstance(storage, S3CompatibleStorageAdapter)
    assert storage._bucket == BUCKET


def test_s3_backend_validation_rejects_missing_credentials() -> None:
    settings = _s3_settings(storage_s3_secret_key="")
    with pytest.raises(ValueError, match="STORAGE_S3_SECRET_KEY"):
        settings.gateway.validate_storage_backend()


def test_worker_metrics_textfile_path_is_shared_storage_default() -> None:
    settings = _s3_settings()
    assert settings.gateway.worker_metrics_file == "/data/storage/metrics/worker.prom"


def test_storage_manifest_script_supports_s3_backend_selection() -> None:
    source = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "storage_manifest.py"),
        encoding="utf-8",
    ).read()
    assert 'settings.gateway.storage_backend == "s3"' in source


def test_storage_drill_script_reports_configured_backend() -> None:
    source = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "storage_drill.py"),
        encoding="utf-8",
    ).read()
    assert '"backend": gateway.storage_backend' in source
