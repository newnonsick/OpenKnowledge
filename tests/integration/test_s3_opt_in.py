from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import pytest

from src.gateway.infrastructure.storage.s3_storage import S3CompatibleStorageAdapter


SPACE_ID = "s3-opt-in-probe"


def _env() -> dict[str, str] | None:
    required = {
        "endpoint": os.environ.get("STORAGE_S3_TEST_ENDPOINT", ""),
        "access_key": os.environ.get("STORAGE_S3_TEST_ACCESS_KEY", ""),
        "secret_key": os.environ.get("STORAGE_S3_TEST_SECRET_KEY", ""),
        "bucket": os.environ.get("STORAGE_S3_TEST_BUCKET", ""),
    }
    if not all(value.strip() for value in required.values()):
        return None
    return {
        "endpoint": required["endpoint"].strip(),
        "bucket": required["bucket"].strip(),
        "region": os.environ.get("STORAGE_S3_TEST_REGION", "us-east-1").strip() or "us-east-1",
        "access_key": required["access_key"].strip(),
        "secret_key": required["secret_key"],
    }


async def _bytes(value: bytes):
    yield value


@pytest.mark.asyncio
async def test_s3_storage_round_trip_against_real_endpoint() -> None:
    config = _env()
    if config is None:
        pytest.skip("STORAGE_S3_TEST_ENDPOINT/KEY/SECRET/BUCKET not set")
    adapter = S3CompatibleStorageAdapter(**config)
    payload = b"s3 opt-in probe " + uuid4().hex.encode()
    upload_id = uuid4()
    document_id = uuid4()
    revision_id = uuid4()
    staged = await adapter.stage(
        space_id=SPACE_ID,
        upload_id=upload_id,
        chunks=_bytes(payload),
        max_bytes=len(payload) + 1024,
    )
    assert staged.storage_key == f"staging/{SPACE_ID}/{upload_id}"
    assert staged.checksum_sha256 == hashlib.sha256(payload).hexdigest()
    try:
        assert await adapter.read(staged.storage_key) == payload
        final_key = await adapter.finalize(
            staged.storage_key,
            space_id=SPACE_ID,
            document_id=document_id,
            revision_id=revision_id,
        )
        assert final_key == f"objects/{SPACE_ID}/{document_id}/{revision_id}"
        assert await adapter.exists(final_key) is True
    finally:
        await adapter.delete(f"objects/{SPACE_ID}/{document_id}/{revision_id}")
        await adapter.delete(staged.storage_key)
