from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from uuid import UUID

from src.gateway.config import get_settings
from src.gateway.infrastructure.storage.factory import build_versioned_object_storage


SPACE_ID = "storage-drill"
UPLOAD_ID = UUID("11111111-1111-4111-8111-111111111111")
DOCUMENT_ID = UUID("22222222-2222-4222-8222-222222222222")
REVISION_ID = UUID("33333333-3333-4333-8333-333333333333")
PAYLOAD = b"storage drill round-trip sentinel"


async def _bytes(value: bytes):
    yield value


async def run() -> dict[str, bool]:
    gateway = get_settings().gateway
    gateway.validate_storage_backend()
    storage = build_versioned_object_storage()
    staged = await storage.stage(
        space_id=SPACE_ID,
        upload_id=UPLOAD_ID,
        chunks=_bytes(PAYLOAD),
        max_bytes=len(PAYLOAD) + 1024,
    )
    staged_key_ok = staged.storage_key == f"staging/{SPACE_ID}/{UPLOAD_ID}"
    checksum_ok = staged.checksum_sha256 == hashlib.sha256(PAYLOAD).hexdigest()
    downloaded = await storage.read(staged.storage_key)
    download_ok = downloaded == PAYLOAD
    final_key = await storage.finalize(
        staged.storage_key,
        space_id=SPACE_ID,
        document_id=DOCUMENT_ID,
        revision_id=REVISION_ID,
    )
    expected_final = f"objects/{SPACE_ID}/{DOCUMENT_ID}/{REVISION_ID}"
    finalize_ok = final_key == expected_final
    staged_gone = not await storage.exists(staged.storage_key)
    inventory = {item.storage_key for item in await storage.list_objects("objects")}
    manifest_ok = expected_final in inventory
    deleted = await storage.delete(final_key)
    deleted_missing = not await storage.delete(final_key)
    exists_gone = not await storage.exists(final_key)
    result = {
        "backend": gateway.storage_backend,
        "staged_key_ok": staged_key_ok,
        "checksum_ok": checksum_ok,
        "download_ok": download_ok,
        "finalize_ok": finalize_ok,
        "staged_gone": staged_gone,
        "manifest_ok": manifest_ok,
        "deleted": deleted,
        "deleted_missing": deleted_missing,
        "exists_gone": exists_gone,
    }
    checks = {name: value for name, value in result.items() if name != "backend"}
    if not all(checks.values()):
        raise RuntimeError(json.dumps(result, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Round-trip stage, download, delete and manifest compare against the configured storage backend.")
    parser.parse_args()
    print(json.dumps(asyncio.run(run()), sort_keys=True))


if __name__ == "__main__":
    main()
