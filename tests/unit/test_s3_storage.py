from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from src.gateway.domain.exceptions import ItemNotFoundException, StorageException, UploadTooLargeException
from src.gateway.infrastructure.storage.s3_storage import S3CompatibleStorageAdapter


BUCKET = "test-bucket"
SPACE_ID = "family"


async def _chunks(*values: bytes):
    for value in values:
        yield value


def _listing(keys):
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


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        self.multipart: dict[str, dict[str, bytes]] = {}
        self.fail_put_keys: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        params = dict(request.url.params)
        prefix = f"/{BUCKET}/"
        path = request.url.path
        key = path[len(prefix):] if path.startswith(prefix) else ""
        if request.method == "HEAD":
            if key in self.objects:
                return httpx.Response(200)
            return httpx.Response(404)
        if request.method == "GET" and not key:
            listed = sorted(
                (name, len(body)) for name, body in self.objects.items() if name.startswith(params.get("prefix", ""))
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
                if key in self.fail_put_keys:
                    return httpx.Response(500, content=b"<Error><Code>InternalError</Code></Error>")
                source = lowered.get("x-amz-copy-source", "")
                decoded = source.replace("%2F", "/")
                source_key = decoded.split(f"/{BUCKET}/", 1)[-1]
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


@pytest.fixture()
def fake_s3():
    return FakeS3()


def _adapter(fake_s3, **overrides):
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake_s3.handler))
    settings = {
        "endpoint": "https://s3.example.test",
        "bucket": BUCKET,
        "region": "us-east-1",
        "access_key": "test-access",
        "secret_key": "test-secret",
        "client": client,
    }
    settings.update(overrides)
    return S3CompatibleStorageAdapter(**settings), client


@pytest.mark.asyncio
async def test_stage_put_uses_sigv4_and_local_key_layout(fake_s3) -> None:
    adapter, _ = _adapter(fake_s3)
    upload_id = uuid4()
    staged = await adapter.stage(
        space_id=SPACE_ID,
        upload_id=upload_id,
        chunks=_chunks(b"hello", b" world"),
        max_bytes=11,
    )
    assert staged.storage_key == f"staging/{SPACE_ID}/{upload_id}"
    assert staged.size_bytes == 11
    assert staged.checksum_sha256 == hashlib.sha256(b"hello world").hexdigest()
    put = next(item for item in fake_s3.requests if item.method == "PUT")
    authorization = put.headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=test-access/")
    assert "SignedHeaders=" in authorization and "Signature=" in authorization
    assert put.headers["x-amz-content-sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert (await adapter.read(staged.storage_key)) == b"hello world"


@pytest.mark.asyncio
async def test_stage_rejects_existing_and_enforces_limit(fake_s3) -> None:
    adapter, _ = _adapter(fake_s3)
    upload_id = uuid4()
    await adapter.stage(space_id=SPACE_ID, upload_id=upload_id, chunks=_chunks(b"data"), max_bytes=64)
    with pytest.raises(StorageException):
        await adapter.stage(space_id=SPACE_ID, upload_id=upload_id, chunks=_chunks(b"data"), max_bytes=64)
    with pytest.raises(UploadTooLargeException):
        await adapter.stage(
            space_id=SPACE_ID,
            upload_id=uuid4(),
            chunks=_chunks(b"123456"),
            max_bytes=5,
        )


@pytest.mark.asyncio
async def test_finalize_copies_then_deletes_staging(fake_s3) -> None:
    adapter, _ = _adapter(fake_s3)
    document_id = uuid4()
    revision_id = uuid4()
    staged = await adapter.stage(
        space_id=SPACE_ID, upload_id=uuid4(), chunks=_chunks(b"payload"), max_bytes=64
    )
    final_key = await adapter.finalize(
        staged.storage_key, space_id=SPACE_ID, document_id=document_id, revision_id=revision_id
    )
    assert final_key == f"objects/{SPACE_ID}/{document_id}/{revision_id}"
    assert await adapter.exists(final_key) is True
    assert await adapter.exists(staged.storage_key) is False
    with pytest.raises(StorageException):
        await adapter.finalize("staging/other-space/x", space_id=SPACE_ID, document_id=document_id, revision_id=revision_id)


@pytest.mark.asyncio
async def test_large_upload_uses_multipart_path(fake_s3) -> None:
    adapter, _ = _adapter(fake_s3, multipart_part_bytes=5 * 1024 * 1024)
    payload = b"x" * (5 * 1024 * 1024 + 7)
    staged = await adapter.stage(
        space_id=SPACE_ID, upload_id=uuid4(), chunks=_chunks(payload), max_bytes=len(payload) + 1
    )
    assert staged.size_bytes == len(payload)
    assert any("partNumber" in dict(item.url.params) for item in fake_s3.requests)
    assert await adapter.read(staged.storage_key) == payload


@pytest.mark.asyncio
async def test_error_mapping_covers_missing_bucket_and_objects(fake_s3) -> None:
    adapter, _ = _adapter(fake_s3)

    def missing_bucket(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"<Error><Code>NoSuchBucket</Code></Error>")

    missing_adapter, _ = _adapter(fake_s3)
    missing_adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(missing_bucket))
    with pytest.raises(StorageException, match="bucket does not exist"):
        await missing_adapter.read("objects/family/doc/rev")
    assert await adapter.exists("objects/family/missing/rev") is False
    assert await adapter.delete("objects/family/missing/rev") is False
    document_id = uuid4()
    revision_id = uuid4()
    fake_s3.fail_put_keys.add(f"objects/family/{document_id}/{revision_id}")
    staged = await adapter.stage(
        space_id="family", upload_id=uuid4(), chunks=_chunks(b"data"), max_bytes=64
    )
    with pytest.raises(StorageException):
        await adapter.finalize(
            staged.storage_key, space_id="family", document_id=document_id, revision_id=revision_id
        )


@pytest.mark.asyncio
async def test_list_objects_parity_with_local_prefixes(fake_s3, tmp_path) -> None:
    from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage

    adapter, _ = _adapter(fake_s3)
    local = LocalVersionedObjectStorage(tmp_path)
    document_id = uuid4()
    revision_id = uuid4()
    for storage in (adapter, local):
        staged = await storage.stage(
            space_id=SPACE_ID, upload_id=uuid4(), chunks=_chunks(b"inventory"), max_bytes=64
        )
        await storage.finalize(
            staged.storage_key, space_id=SPACE_ID, document_id=document_id, revision_id=revision_id
        )
    s3_keys = [item.storage_key for item in await adapter.list_objects("objects")]
    local_keys = [item.storage_key for item in await local.list_objects("objects")]
    assert s3_keys == local_keys == [f"objects/{SPACE_ID}/{document_id}/{revision_id}"]
    with pytest.raises(StorageException):
        await adapter.list_objects("invalid")


@pytest.mark.asyncio
async def test_read_missing_key_raises_not_found(fake_s3) -> None:
    adapter, _ = _adapter(fake_s3)
    with pytest.raises(ItemNotFoundException):
        await adapter.read("objects/family/missing/rev")


@pytest.mark.asyncio
async def test_sigv4_scope_and_signature_format(fake_s3) -> None:
    adapter, _ = _adapter(fake_s3)
    staged = await adapter.stage(
        space_id=SPACE_ID, upload_id=uuid4(), chunks=_chunks(b"vector"), max_bytes=64
    )
    put = next(item for item in fake_s3.requests if item.method == "PUT")
    authorization = put.headers["authorization"]
    datestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert f"Credential=test-access/{datestamp}/us-east-1/s3/aws4_request" in authorization
    signature = authorization.rsplit("Signature=", 1)[-1]
    assert len(signature) == 64 and all(char in "0123456789abcdef" for char in signature)
    assert await adapter.read(staged.storage_key) == b"vector"


@pytest.mark.asyncio
async def test_constructor_validates_configuration() -> None:
    with pytest.raises(ValueError):
        S3CompatibleStorageAdapter(
            endpoint="not-a-url", bucket=BUCKET, region="us-east-1", access_key="a", secret_key="s"
        )
    with pytest.raises(ValueError):
        S3CompatibleStorageAdapter(
            endpoint="https://s3.example.test",
            bucket="",
            region="us-east-1",
            access_key="a",
            secret_key="s",
        )
    with pytest.raises(ValueError):
        S3CompatibleStorageAdapter(
            endpoint="https://s3.example.test",
            bucket=BUCKET,
            region="",
            access_key="a",
            secret_key="s",
        )
    with pytest.raises(ValueError):
        S3CompatibleStorageAdapter(
            endpoint="https://s3.example.test",
            bucket=BUCKET,
            region="us-east-1",
            access_key="",
            secret_key="s",
        )
    with pytest.raises(ValueError):
        S3CompatibleStorageAdapter(
            endpoint="https://s3.example.test",
            bucket=BUCKET,
            region="us-east-1",
            access_key="a",
            secret_key="",
        )
