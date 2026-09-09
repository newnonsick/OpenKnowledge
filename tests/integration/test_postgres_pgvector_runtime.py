from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.gateway.config import get_settings
from src.gateway.infrastructure.database import normalize_database_url
from src.gateway.infrastructure.migrations import run_migrations_async


async def test_configured_postgres_pgvector_round_trip_and_cosine_ordering() -> None:
    await run_migrations_async()
    engine = create_async_engine(
        normalize_database_url(get_settings().database.url),
        poolclass=NullPool,
    )
    workspace_id = f"vector-test-{uuid4().hex}"
    first_item = uuid4()
    second_item = uuid4()
    first_revision = uuid4()
    second_revision = uuid4()
    dimension = get_settings().embedding.dimension
    near = "[" + ",".join(["1", "0"] + ["0"] * (dimension - 2)) + "]"
    far = "[" + ",".join(["0", "1"] + ["0"] * (dimension - 2)) + "]"

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                extension_version = await connection.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
                column_type = await connection.scalar(
                    text(
                        "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                        "JOIN pg_class c ON c.oid = a.attrelid "
                        "WHERE c.relname = 'knowledge_revisions' AND a.attname = 'embedding' "
                        "AND NOT a.attisdropped"
                    )
                )
                vector_cast = "halfvec" if (column_type or "").startswith("halfvec") else "vector"
                indexes = set(
                    (
                        await connection.execute(
                            text(
                                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
                                "AND indexname IN ('ix_knowledge_revisions_embedding','ix_document_chunks_embedding')"
                            )
                        )
                    ).scalars()
                )
                assert extension_version is not None
                assert indexes == {
                    "ix_knowledge_revisions_embedding",
                    "ix_document_chunks_embedding",
                }

                await connection.execute(
                    text("INSERT INTO workspaces (id, name) VALUES (:id, :name)"),
                    {"id": workspace_id, "name": "Vector Runtime Test"},
                )
                await connection.execute(
                    text(
                        "INSERT INTO knowledge_items "
                        "(id, workspace_id, title, content, is_global, is_deleted) VALUES "
                        "(:first_id, :workspace_id, 'Near', 'near', false, false), "
                        "(:second_id, :workspace_id, 'Far', 'far', false, false)"
                    ),
                    {
                        "first_id": first_item,
                        "second_id": second_item,
                        "workspace_id": workspace_id,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO knowledge_revisions "
                        "(id, item_id, space_id, version, title, content_hash, content, embedding, author) VALUES "
                        f"(:first_revision, :first_item, :workspace_id, 1, 'Near', :first_hash, 'near', CAST(:near AS {vector_cast}), 'test'), "
                        f"(:second_revision, :second_item, :workspace_id, 1, 'Far', :second_hash, 'far', CAST(:far AS {vector_cast}), 'test')"
                    ),
                    {
                        "first_revision": first_revision,
                        "first_item": first_item,
                        "workspace_id": workspace_id,
                        "first_hash": "a" * 64,
                        "near": near,
                        "second_revision": second_revision,
                        "second_item": second_item,
                        "second_hash": "b" * 64,
                        "far": far,
                    },
                )
                rows = (
                    await connection.execute(
                        text(
                            f"SELECT item_id, embedding <=> CAST(:query AS {vector_cast}) AS distance "
                            "FROM knowledge_revisions WHERE item_id IN (:first_item, :second_item) "
                            "ORDER BY distance ASC"
                        ),
                        {
                            "query": near,
                            "first_item": first_item,
                            "second_item": second_item,
                        },
                    )
                ).all()
                assert [row.item_id for row in rows] == [first_item, second_item]
                assert rows[0].distance == 0.0
                assert rows[1].distance > rows[0].distance
            finally:
                await transaction.rollback()

        async with engine.connect() as connection:
            remaining = await connection.scalar(
                text("SELECT count(*) FROM workspaces WHERE id = :id"),
                {"id": workspace_id},
            )
            assert remaining == 0
    finally:
        await engine.dispose()
