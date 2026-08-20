import re
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.gateway.infrastructure.database import validate_worker_database_connection
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_worker_role_is_privileged_only_for_fenced_background_work() -> None:
    worker_role = f"akg_worker_{uuid4().hex[:12]}"
    assert re.fullmatch(r"akg_worker_[0-9a-f]{12}", worker_role)
    async with isolated_postgres_database() as (engine, _):
        async with engine.begin() as connection:
            can_create_role = await connection.scalar(
                text(
                    "SELECT rolsuper OR rolcreaterole FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
            if not can_create_role:
                pytest.skip("Configured PostgreSQL role cannot create a BYPASSRLS worker role")
            await connection.execute(
                text(f'CREATE ROLE "{worker_role}" NOLOGIN BYPASSRLS')
            )
            await connection.execute(text(f'GRANT "{worker_role}" TO CURRENT_USER'))
            await connection.execute(
                text(f'GRANT USAGE ON SCHEMA public TO "{worker_role}"')
            )
            await connection.execute(
                text(
                    "GRANT SELECT ON document_revision_chunks, document_revisions, documents, "
                    "embedding_generations, ingestion_jobs, job_outbox, members, retrieval_units, "
                    f'operational_alerts, space_memberships, workspaces TO "{worker_role}"'
                )
            )
            await connection.execute(
                text(
                    "GRANT INSERT ON document_revision_chunks, job_outbox, operational_alerts, retrieval_units "
                    f'TO "{worker_role}"'
                )
            )
            await connection.execute(
                text(
                    "GRANT UPDATE (current_revision_id, revision, updated_at) "
                    f'ON documents TO "{worker_role}"'
                )
            )
            await connection.execute(
                text(
                    "GRANT UPDATE (staging_storage_key, parser_version, status, failure_code, ready_at, activated_at) "
                    f'ON document_revisions TO "{worker_role}"'
                )
            )
            await connection.execute(
                text(
                    "GRANT UPDATE (state, progress, attempt_count, next_attempt_at, cancellation_requested, retry_requested, "
                    "last_error_code, last_error_detail, lease_owner, lease_expires_at, claim_token, "
                    "updated_at, started_at, finished_at) "
                    f'ON ingestion_jobs TO "{worker_role}"'
                )
            )
            await connection.execute(
                text(
                    "GRANT UPDATE (state, attempt_count, last_error_code, lease_owner, "
                    "lease_expires_at, claim_token, available_at, published_at, updated_at) "
                    f'ON job_outbox TO "{worker_role}"'
                )
            )
            await connection.execute(
                text(
                    "GRANT UPDATE (active, deactivated_at) "
                    f'ON retrieval_units TO "{worker_role}"'
                )
            )

        try:
            async with engine.connect() as connection:
                await connection.execute(text(f'SET ROLE "{worker_role}"'))
                await connection.commit()
                await validate_worker_database_connection(connection)
                assert not await connection.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, "
                        "'public.personal_api_keys', 'SELECT')"
                    )
                )
                assert not await connection.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, "
                        "'public.document_revisions', 'DELETE')"
                    )
                )
        finally:
            async with engine.begin() as connection:
                await connection.execute(text("RESET ROLE"))
                await connection.execute(
                    text(f'DROP OWNED BY "{worker_role}"')
                )
                await connection.execute(
                    text(f'DROP ROLE IF EXISTS "{worker_role}"')
                )
