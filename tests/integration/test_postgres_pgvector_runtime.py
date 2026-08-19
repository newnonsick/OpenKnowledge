from uuid import uuid4

from sqlalchemy import text

from src.gateway.infrastructure.database import get_engine
from src.gateway.infrastructure.migrations import run_migrations_async


async def test_configured_postgres_pgvector_round_trip_and_cosine_ordering() -> None:
    await run_migrations_async()
    engine = get_engine()
    workspace_id = f"vector-test-{uuid4().hex}"
    first_item = uuid4()
    second_item = uuid4()
    first_revision = uuid4()
    second_revision = uuid4()
    near = "[" + ",".join(["1", "0"] + ["0"] * 1022) + "]"
    far = "[" + ",".join(["0", "1"] + ["0"] * 1022) + "]"

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            extension_version = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
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
            assert indexes == {"ix_knowledge_revisions_embedding", "ix_document_chunks_embedding"}

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
                {"first_id": first_item, "second_id": second_item, "workspace_id": workspace_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO knowledge_revisions "
                    "(id, item_id, version, content_hash, content, embedding, author) VALUES "
                    "(:first_revision, :first_item, 1, :first_hash, 'near', CAST(:near AS vector), 'test'), "
                    "(:second_revision, :second_item, 1, :second_hash, 'far', CAST(:far AS vector), 'test')"
                ),
                {
                    "first_revision": first_revision,
                    "first_item": first_item,
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
                        "SELECT item_id, embedding <=> CAST(:query AS vector) AS distance "
                        "FROM knowledge_revisions WHERE item_id IN (:first_item, :second_item) "
                        "ORDER BY distance ASC"
                    ),
                    {"query": near, "first_item": first_item, "second_item": second_item},
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
