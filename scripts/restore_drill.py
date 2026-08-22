from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.database import normalize_database_url, principal_session
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionChunkModel, DocumentRevisionModel, EmbeddingGenerationModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage


MEMBER_ID = UUID("8a47e92d-8d49-4c85-88e4-7700d599dc11")
KNOWLEDGE_ID = UUID("6f09788f-3c73-4b4e-a625-987d62b6058a")
KNOWLEDGE_REVISION_ID = UUID("39031e46-bb28-4d3f-a578-e5ce789a9c1d")
DOCUMENT_ID = UUID("4cb9ec4f-e133-49c4-8091-947c4af8a824")
DOCUMENT_REVISION_ID = UUID("98d6b84a-6fe9-4e76-ab6b-f88b844cf1b7")
DOCUMENT_CHUNK_ID = UUID("2b00d108-9526-4488-9408-1318417adb92")
GENERATION_ID = UUID("9c692133-edf0-466a-a7b9-2f7f805aaf0d")
UPLOAD_ID = UUID("94c8dfa7-b007-48fe-8541-84973fa97148")
SPACE_ID = "restore-drill"
KNOWLEDGE_CONTENT = "restore lexical sentinel ตารางครอบครัว family schedule"
DOCUMENT_CONTENT = b"immutable restore object sentinel"


async def _bytes(value: bytes):
    yield value


def _vector() -> list[float]:
    return [1.0] + [0.0] * (EMBED_DIM - 1)


def _principal() -> Principal:
    return Principal(
        subject_id=str(MEMBER_ID),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read"}),
    )


async def seed(factory: async_sessionmaker, storage: LocalVersionedObjectStorage) -> dict[str, bool]:
    storage_key = f"objects/{SPACE_ID}/{DOCUMENT_ID}/{DOCUMENT_REVISION_ID}"
    if not await storage.exists(storage_key):
        staged = await storage.stage(
            space_id=SPACE_ID,
            upload_id=UPLOAD_ID,
            chunks=_bytes(DOCUMENT_CONTENT),
            max_bytes=1024,
        )
        storage_key = await storage.finalize(
            staged.storage_key,
            space_id=SPACE_ID,
            document_id=DOCUMENT_ID,
            revision_id=DOCUMENT_REVISION_ID,
        )
    now = datetime.now(timezone.utc)
    checksum = hashlib.sha256(DOCUMENT_CONTENT).hexdigest()
    async with factory.begin() as session:
        if await session.get(MemberModel, MEMBER_ID) is None:
            session.add(
                MemberModel(
                    id=MEMBER_ID,
                    username="restore-drill",
                    username_normalized="restore-drill",
                    display_name="Restore Drill",
                    status="active",
                    system_role="member",
                    force_password_change=False,
                )
            )
            session.add(Workspace(id=SPACE_ID, name="Restore Drill", created_by_member_id=MEMBER_ID))
            await session.flush()
            session.add(SpaceMembershipModel(space_id=SPACE_ID, member_id=MEMBER_ID, role="owner"))
            session.add(
                EmbeddingGenerationModel(
                    id=GENERATION_ID,
                    purpose="retrieval",
                    model_id="restore-drill-deterministic",
                    dimensions=EMBED_DIM,
                    status="active",
                    activated_at=now,
                )
            )
            knowledge = KnowledgeItem(
                id=KNOWLEDGE_ID,
                workspace_id=SPACE_ID,
                title="Restore Drill Knowledge",
                content=KNOWLEDGE_CONTENT,
                current_revision_id=None,
                tags=["restore-drill"],
                is_global=False,
                is_deleted=False,
            )
            session.add(knowledge)
            await session.flush()
            session.add(
                KnowledgeRevision(
                    id=KNOWLEDGE_REVISION_ID,
                    item_id=KNOWLEDGE_ID,
                    space_id=SPACE_ID,
                    version=1,
                    title="Restore Drill Knowledge",
                    content_hash=hashlib.sha256(KNOWLEDGE_CONTENT.encode()).hexdigest(),
                    content=KNOWLEDGE_CONTENT,
                    tags=["restore-drill"],
                    embedding=_vector(),
                    author="restore-drill",
                    author_member_id=MEMBER_ID,
                )
            )
            await session.flush()
            knowledge.current_revision_id = KNOWLEDGE_REVISION_ID
            document = DocumentModel(
                id=DOCUMENT_ID,
                space_id=SPACE_ID,
                display_name="Restore Drill Document",
                current_revision_id=None,
                created_by_member_id=MEMBER_ID,
            )
            session.add(document)
            await session.flush()
            session.add(
                DocumentRevisionModel(
                    id=DOCUMENT_REVISION_ID,
                    document_id=DOCUMENT_ID,
                    space_id=SPACE_ID,
                    version=1,
                    original_filename="restore-drill.txt",
                    mime_type="text/plain",
                    size_bytes=len(DOCUMENT_CONTENT),
                    checksum_sha256=checksum,
                    storage_key=storage_key,
                    parser_version="restore-drill-v1",
                    status="active",
                    created_by_member_id=MEMBER_ID,
                    ready_at=now,
                    activated_at=now,
                )
            )
            await session.flush()
            document.current_revision_id = DOCUMENT_REVISION_ID
            session.add(
                DocumentRevisionChunkModel(
                    id=DOCUMENT_CHUNK_ID,
                    document_revision_id=DOCUMENT_REVISION_ID,
                    document_id=DOCUMENT_ID,
                    space_id=SPACE_ID,
                    chunk_index=0,
                    content=DOCUMENT_CONTENT.decode(),
                    content_hash=checksum,
                    language="en",
                )
            )
            session.add_all(
                [
                    RetrievalUnitModel(
                        space_id=SPACE_ID,
                        source_type="knowledge_revision",
                        knowledge_revision_id=KNOWLEDGE_REVISION_ID,
                        embedding_generation_id=GENERATION_ID,
                        title="Restore Drill Knowledge",
                        content=KNOWLEDGE_CONTENT,
                        language="mixed",
                        embedding=_vector(),
                        active=True,
                    ),
                    RetrievalUnitModel(
                        space_id=SPACE_ID,
                        source_type="document_chunk",
                        document_revision_chunk_id=DOCUMENT_CHUNK_ID,
                        embedding_generation_id=GENERATION_ID,
                        title="Restore Drill Document",
                        content=DOCUMENT_CONTENT.decode(),
                        language="en",
                        embedding=None,
                        active=True,
                    ),
                ]
            )
    return {"seeded": True}


async def verify(factory: async_sessionmaker, storage: LocalVersionedObjectStorage) -> dict[str, bool]:
    async with principal_session(factory, _principal()) as session:
        knowledge = await session.get(KnowledgeItem, KNOWLEDGE_ID)
        revision = await session.get(DocumentRevisionModel, DOCUMENT_REVISION_ID)
    canonical_knowledge = bool(
        knowledge
        and knowledge.current_revision_id == KNOWLEDGE_REVISION_ID
        and knowledge.content == KNOWLEDGE_CONTENT
    )
    immutable_storage = bool(
        revision
        and revision.storage_key
        and await storage.read(revision.storage_key) == DOCUMENT_CONTENT
        and revision.checksum_sha256 == hashlib.sha256(DOCUMENT_CONTENT).hexdigest()
    )
    repository = PostgresRetrievalUnitRepository(factory)
    lexical = await repository.lexical_search(
        _principal(),
        [SPACE_ID],
        "ตารางครอบครัว",
        GENERATION_ID,
        10,
        0.01,
    )
    vector = await repository.vector_search(
        _principal(),
        [SPACE_ID],
        _vector(),
        GENERATION_ID,
        10,
        0.99,
        True,
        100,
    )
    approximate = await repository.vector_search(
        _principal(),
        [SPACE_ID],
        _vector(),
        GENERATION_ID,
        10,
        0.99,
        False,
        100,
    )
    plan = await repository.vector_search_plan(
        _principal(),
        [SPACE_ID],
        _vector(),
        GENERATION_ID,
        10,
        0.99,
        100,
    )
    result = {
        "canonical_knowledge": canonical_knowledge,
        "immutable_storage": immutable_storage,
        "lexical_retrieval": any(item.canonical_id == KNOWLEDGE_ID for item in lexical),
        "vector_retrieval": any(item.canonical_id == KNOWLEDGE_ID for item in vector),
        "ann_retrieval": any(item.canonical_id == KNOWLEDGE_ID for item in approximate),
        "hnsw_plan": "ix_retrieval_units_embedding" in json.dumps(plan),
    }
    if not all(result.values()):
        raise RuntimeError(json.dumps(result, sort_keys=True))
    return result


async def run(action: str, database_url: str, storage_dir: Path) -> None:
    engine = create_async_engine(normalize_database_url(database_url), pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = LocalVersionedObjectStorage(storage_dir)
    try:
        result = await (seed(factory, storage) if action == "seed" else verify(factory, storage))
        print(json.dumps(result, sort_keys=True))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("seed", "verify"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--storage-dir", required=True, type=Path)
    arguments = parser.parse_args()
    asyncio.run(run(arguments.action, arguments.database_url, arguments.storage_dir))


if __name__ == "__main__":
    main()
