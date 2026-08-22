from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.gateway.application.services.storage_manifest_service import StorageManifest, StorageManifestService
from src.gateway.infrastructure.database import normalize_database_url
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage


async def run(action: str, database_url: str, storage_dir: Path, manifest_path: Path) -> None:
    engine = create_async_engine(normalize_database_url(database_url), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = StorageManifestService(factory, LocalVersionedObjectStorage(storage_dir))
    try:
        if action == "create":
            manifest = await service.create()
            manifest_path.write_text(manifest.to_json(), encoding="utf-8")
            return
        manifest = StorageManifest.from_json(manifest_path.read_text(encoding="utf-8"))
        verification = await service.verify(manifest)
        if not verification.valid:
            raise RuntimeError(
                f"Storage manifest verification failed: missing={len(verification.missing_keys)}, "
                f"size={len(verification.invalid_size_keys)}, checksum={len(verification.invalid_checksum_keys)}, "
                f"database_missing_manifest={len(verification.database_missing_manifest_keys)}, "
                f"manifest_missing_database={len(verification.manifest_missing_database_keys)}, "
                f"duplicates={len(verification.duplicate_manifest_keys)}, "
                f"manifest_digest={verification.manifest_digest_valid}"
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--storage-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    arguments = parser.parse_args()
    asyncio.run(run(arguments.action, arguments.database_url, arguments.storage_dir, arguments.manifest))


if __name__ == "__main__":
    main()
