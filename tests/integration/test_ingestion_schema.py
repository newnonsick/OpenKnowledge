from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.gateway.config import get_settings
from src.gateway.infrastructure.database import normalize_database_url
from src.gateway.infrastructure.migrations import get_schema_status_async, run_migrations_async


async def test_configured_postgres_has_durable_ingestion_schema() -> None:
    await run_migrations_async()
    status = await get_schema_status_async(expected_embedding_dimension=1024)
    assert status.compatible is True
    assert status.current_revision == "020"

    engine = create_async_engine(
        normalize_database_url(get_settings().database.url),
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            tables = set(
                await connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()"
                    )
                )
            )
            assert {
                "documents",
                "document_revisions",
                "document_revision_chunks",
                "embedding_generations",
                "retrieval_units",
                "provenance_links",
                "ingestion_jobs",
                "job_outbox",
            } <= tables

            constraints = set(
                await connection.scalars(
                    text(
                        "SELECT conname FROM pg_constraint c "
                        "JOIN pg_namespace n ON n.oid = c.connamespace "
                        "WHERE n.nspname = current_schema()"
                    )
                )
            )
            assert {
                "fk_knowledge_items_current_revision_parent_space",
                "fk_knowledge_revisions_item_space",
                "uq_knowledge_revisions_id_space",
                "uq_document_revisions_document_version",
                "uq_document_revision_chunks_revision_index",
                "ck_document_revisions_status",
                "ck_ingestion_jobs_state",
                "ck_ingestion_jobs_claim",
                "ck_retrieval_units_source",
                "ck_provenance_links_source",
            } <= constraints

            knowledge_columns = set(
                await connection.scalars(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() "
                        "AND table_name = 'knowledge_revisions'"
                    )
                )
            )
            assert {
                "space_id",
                "title",
                "tags",
                "change_summary",
                "author_member_id",
            } <= knowledge_columns

            forced_tables = set(
                await connection.scalars(
                    text(
                        "SELECT relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = current_schema() "
                        "AND c.relrowsecurity AND c.relforcerowsecurity"
                    )
                )
            )
            assert {
                "documents",
                "document_revisions",
                "document_revision_chunks",
                "retrieval_units",
                "provenance_links",
                "ingestion_jobs",
            } <= forced_tables
    finally:
        await engine.dispose()
