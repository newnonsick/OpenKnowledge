from __future__ import annotations

from src.gateway.config import get_settings
from src.gateway.application.ports.object_storage import IVersionedObjectStorage
from src.gateway.infrastructure.storage.s3_storage import S3CompatibleStorageAdapter
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage


def build_versioned_object_storage(settings=None) -> IVersionedObjectStorage:
    resolved = settings or get_settings()
    gateway = resolved.gateway
    gateway.validate_storage_backend()
    if gateway.storage_backend == "s3":
        return S3CompatibleStorageAdapter(
            endpoint=gateway.storage_s3_endpoint or "",
            bucket=gateway.storage_s3_bucket,
            region=gateway.storage_s3_region,
            access_key=gateway.storage_s3_access_key,
            secret_key=gateway.storage_s3_secret_key,
            session_token=gateway.storage_s3_session_token,
            prefix=gateway.storage_s3_prefix,
        )
    return LocalVersionedObjectStorage(gateway.storage_dir)
