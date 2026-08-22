from __future__ import annotations

import pytest

from scripts.recovery_verify import verify_recovery_database
from scripts.restore_drill import DOCUMENT_CONTENT, DOCUMENT_ID, DOCUMENT_REVISION_ID, SPACE_ID, seed, verify
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_restore_drill_verifies_canonical_storage_and_retrieval(tmp_path) -> None:
    async with isolated_postgres_database() as (engine, factory):
        storage = LocalVersionedObjectStorage(tmp_path)
        assert await seed(factory, storage) == {"seeded": True}
        assert await verify(factory, storage) == {
            "canonical_knowledge": True,
            "immutable_storage": True,
            "lexical_retrieval": True,
            "vector_retrieval": True,
            "ann_retrieval": True,
            "hnsw_plan": True,
        }
        assert await storage.read(
            f"objects/{SPACE_ID}/{DOCUMENT_ID}/{DOCUMENT_REVISION_ID}"
        ) == DOCUMENT_CONTENT
        assert await verify_recovery_database(
            engine.url.render_as_string(hide_password=False)
        ) == {
            "canonical_pointers": True,
            "retrieval_sources": True,
            "lexical_retrieval": True,
            "vector_retrieval": True,
        }


async def test_restore_drill_rejects_corrupt_immutable_storage(tmp_path) -> None:
    async with isolated_postgres_database() as (_, factory):
        storage = LocalVersionedObjectStorage(tmp_path)
        await seed(factory, storage)
        path = tmp_path / "objects" / SPACE_ID / str(DOCUMENT_ID) / str(DOCUMENT_REVISION_ID)
        path.write_bytes(b"x" * len(DOCUMENT_CONTENT))

        with pytest.raises(RuntimeError, match='"immutable_storage": false'):
            await verify(factory, storage)
