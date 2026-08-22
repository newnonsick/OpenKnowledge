from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.gateway.infrastructure.database import normalize_database_url


async def verify_recovery_database(database_url: str) -> dict[str, bool]:
    engine = create_async_engine(normalize_database_url(database_url), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            invalid_knowledge_pointers = int(
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM knowledge_items item "
                        "LEFT JOIN knowledge_revisions revision "
                        "ON revision.id = item.current_revision_id "
                        "AND revision.item_id = item.id AND revision.space_id = item.workspace_id "
                        "WHERE item.current_revision_id IS NOT NULL AND revision.id IS NULL"
                    )
                )
                or 0
            )
            invalid_document_pointers = int(
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM documents document "
                        "LEFT JOIN document_revisions revision "
                        "ON revision.id = document.current_revision_id "
                        "AND revision.document_id = document.id AND revision.space_id = document.space_id "
                        "WHERE document.current_revision_id IS NOT NULL AND revision.id IS NULL"
                    )
                )
                or 0
            )
            invalid_retrieval_sources = int(
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM retrieval_units unit "
                        "LEFT JOIN knowledge_revisions knowledge "
                        "ON knowledge.id = unit.knowledge_revision_id AND knowledge.space_id = unit.space_id "
                        "LEFT JOIN document_revision_chunks chunk "
                        "ON chunk.id = unit.document_revision_chunk_id AND chunk.space_id = unit.space_id "
                        "WHERE (unit.source_type = 'knowledge_revision' AND knowledge.id IS NULL) "
                        "OR (unit.source_type = 'document_chunk' AND chunk.id IS NULL)"
                    )
                )
                or 0
            )
            sample = (
                await connection.execute(
                    text(
                        "SELECT id, title, embedding::text AS embedding "
                        "FROM retrieval_units WHERE active AND embedding IS NOT NULL "
                        "ORDER BY id LIMIT 1"
                    )
                )
            ).mappings().one_or_none()
            lexical_retrieval = False
            vector_retrieval = False
            if sample is not None:
                lexical_retrieval = bool(
                    await connection.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM retrieval_units "
                            "WHERE id = :unit_id AND active "
                            "AND (tsv @@ plainto_tsquery('simple', :title) "
                            "OR title ILIKE ('%' || :title || '%')))"
                        ),
                        {"unit_id": sample["id"], "title": sample["title"]},
                    )
                )
                nearest = await connection.scalar(
                    text(
                        "SELECT id FROM retrieval_units "
                        "WHERE active AND embedding IS NOT NULL "
                        "ORDER BY embedding <=> CAST(:embedding AS vector), id LIMIT 1"
                    ),
                    {"embedding": sample["embedding"]},
                )
                vector_retrieval = nearest == sample["id"]
        result = {
            "canonical_pointers": invalid_knowledge_pointers == 0
            and invalid_document_pointers == 0,
            "retrieval_sources": invalid_retrieval_sources == 0,
            "lexical_retrieval": lexical_retrieval,
            "vector_retrieval": vector_retrieval,
        }
        if not all(result.values()):
            raise RuntimeError(json.dumps(result, sort_keys=True))
        return result
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    arguments = parser.parse_args()
    result = asyncio.run(verify_recovery_database(arguments.database_url))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
