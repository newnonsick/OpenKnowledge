from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integration.postgres_test_database import isolated_postgres_database


async def test_knowledge_revision_parent_and_space_are_database_enforced() -> None:
    async with isolated_postgres_database() as (engine, _):
        space_a = f"knowledge-a-{uuid4().hex[:8]}"
        space_b = f"knowledge-b-{uuid4().hex[:8]}"
        item_a = uuid4()
        item_b = uuid4()
        revision_a = uuid4()
        revision_b = uuid4()

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO workspaces (id, name) VALUES "
                    "(:space_a, 'Knowledge A'), (:space_b, 'Knowledge B')"
                ),
                {"space_a": space_a, "space_b": space_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO knowledge_items "
                    "(id, workspace_id, title, content) VALUES "
                    "(:item_a, :space_a, 'A', 'A'), (:item_b, :space_b, 'B', 'B')"
                ),
                {
                    "item_a": item_a,
                    "space_a": space_a,
                    "item_b": item_b,
                    "space_b": space_b,
                },
            )

            nested = await connection.begin_nested()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "INSERT INTO knowledge_revisions "
                        "(id, item_id, space_id, version, content_hash, content) "
                        "VALUES (:id, :item_id, :space_id, 1, :hash, 'invalid')"
                    ),
                    {
                        "id": uuid4(),
                        "item_id": item_a,
                        "space_id": space_b,
                        "hash": "a" * 64,
                    },
                )
            await nested.rollback()

            await connection.execute(
                text(
                    "INSERT INTO knowledge_revisions "
                    "(id, item_id, space_id, version, content_hash, content) VALUES "
                    "(:revision_a, :item_a, :space_a, 1, :hash_a, 'A'), "
                    "(:revision_b, :item_b, :space_b, 1, :hash_b, 'B')"
                ),
                {
                    "revision_a": revision_a,
                    "item_a": item_a,
                    "space_a": space_a,
                    "hash_a": "a" * 64,
                    "revision_b": revision_b,
                    "item_b": item_b,
                    "space_b": space_b,
                    "hash_b": "b" * 64,
                },
            )
            await connection.execute(
                text(
                    "UPDATE knowledge_items SET current_revision_id = :revision_a "
                    "WHERE id = :item_a"
                ),
                {"revision_a": revision_a, "item_a": item_a},
            )

            nested = await connection.begin_nested()
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        "UPDATE knowledge_items SET current_revision_id = :revision_b "
                        "WHERE id = :item_a"
                    ),
                    {"revision_b": revision_b, "item_a": item_a},
                )
            await nested.rollback()
