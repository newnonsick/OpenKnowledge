from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RETENTION_FUNCTION = """
CREATE FUNCTION gateway_run_retention(
    archived_before timestamptz,
    revision_before timestamptz,
    operational_before timestamptz,
    max_rows integer
) RETURNS TABLE(record_type text, record_id text, storage_key text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    affected bigint := 0;
    total_affected bigint := 0;
    target_knowledge uuid;
    target_document uuid;
    target_revision uuid;
    target_family uuid;
    target_key uuid;
BEGIN
    IF max_rows IS NULL OR max_rows < 1 OR max_rows > 1000 THEN
        RAISE EXCEPTION 'Retention batch size must be between 1 and 1000';
    END IF;
    IF archived_before > statement_timestamp()
       OR revision_before > statement_timestamp()
       OR operational_before > statement_timestamp() THEN
        RAISE EXCEPTION 'Retention cutoffs cannot be in the future';
    END IF;
    IF archived_before > statement_timestamp() - INTERVAL '365 days'
       OR revision_before > statement_timestamp() - INTERVAL '1095 days'
       OR operational_before > statement_timestamp() - INTERVAL '30 days' THEN
        RAISE EXCEPTION 'Retention cutoffs violate the database safety floor';
    END IF;
    PERFORM pg_advisory_xact_lock(739814621451062318);

    INSERT INTO public.retention_purge_jobs (root_type, root_id, archived_at)
    SELECT 'knowledge_item', item.id, item.archived_at
    FROM public.knowledge_items item
    WHERE item.archived_at <= archived_before
      AND NOT EXISTS (
          SELECT 1 FROM public.retention_purge_jobs job
          WHERE job.root_type = 'knowledge_item' AND job.root_id = item.id
      )
    ORDER BY item.archived_at, item.id
    LIMIT 1
    ON CONFLICT DO NOTHING;

    INSERT INTO public.retention_purge_jobs (root_type, root_id, archived_at)
    SELECT 'document', document.id, document.archived_at
    FROM public.documents document
    WHERE document.archived_at <= archived_before
      AND NOT EXISTS (
          SELECT 1 FROM public.retention_purge_jobs job
          WHERE job.root_type = 'document' AND job.root_id = document.id
      )
      AND NOT EXISTS (
          SELECT 1
          FROM public.provenance_links link
          LEFT JOIN public.document_revision_chunks chunk
            ON chunk.id = link.document_revision_chunk_id
          LEFT JOIN public.document_revisions revision
            ON revision.id = link.document_revision_id
            OR revision.id = chunk.document_revision_id
          WHERE revision.document_id = document.id
      )
    ORDER BY document.archived_at, document.id
    LIMIT 1
    ON CONFLICT DO NOTHING;

    SELECT item.id INTO target_knowledge
    FROM public.retention_purge_jobs job
    JOIN public.knowledge_items item ON item.id = job.root_id
    WHERE job.root_type = 'knowledge_item' AND item.archived_at <= archived_before
    ORDER BY job.archived_at, job.root_id
    FOR UPDATE OF job, item SKIP LOCKED
    LIMIT 1;
    IF target_knowledge IS NOT NULL THEN
        UPDATE public.knowledge_items
        SET current_revision_id = NULL
        WHERE id = target_knowledge AND current_revision_id IS NOT NULL;
    END IF;

    SELECT revision.id INTO target_revision
    FROM public.knowledge_revisions revision
    JOIN public.knowledge_items item ON item.id = revision.item_id
    WHERE (
        target_knowledge IS NOT NULL AND revision.item_id = target_knowledge
    ) OR (
        revision.created_at <= revision_before
        AND revision.id IS DISTINCT FROM item.current_revision_id
    )
    ORDER BY (revision.item_id = target_knowledge) DESC, revision.created_at, revision.id
    FOR UPDATE OF revision SKIP LOCKED
    LIMIT 1;
    IF target_revision IS NOT NULL THEN
        RETURN QUERY
        WITH candidates AS MATERIALIZED (
            SELECT unit.id FROM public.retrieval_units unit
            WHERE unit.knowledge_revision_id = target_revision
            ORDER BY unit.id LIMIT max_rows
        ), deleted AS (
            DELETE FROM public.retrieval_units unit USING candidates candidate
            WHERE unit.id = candidate.id RETURNING unit.id
        )
        SELECT 'retrieval_unit'::text, deleted.id::text, NULL::text FROM deleted;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;

        RETURN QUERY
        WITH candidates AS MATERIALIZED (
            SELECT link.id FROM public.provenance_links link
            WHERE link.knowledge_revision_id = target_revision
            ORDER BY link.id LIMIT max_rows
        ), deleted AS (
            DELETE FROM public.provenance_links link USING candidates candidate
            WHERE link.id = candidate.id RETURNING link.id
        )
        SELECT 'provenance_link'::text, deleted.id::text, NULL::text FROM deleted;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;

        RETURN QUERY
        DELETE FROM public.knowledge_revisions revision
        WHERE revision.id = target_revision
          AND NOT EXISTS (
              SELECT 1 FROM public.retrieval_units unit
              WHERE unit.knowledge_revision_id = revision.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM public.provenance_links link
              WHERE link.knowledge_revision_id = revision.id
          )
        RETURNING 'knowledge_revision'::text, revision.id::text, NULL::text;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;
    END IF;

    IF target_knowledge IS NOT NULL THEN
        RETURN QUERY
        DELETE FROM public.knowledge_items item
        WHERE item.id = target_knowledge
          AND item.current_revision_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM public.knowledge_revisions revision
              WHERE revision.item_id = item.id
          )
        RETURNING 'knowledge_item'::text, item.id::text, NULL::text;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;
        IF affected > 0 THEN
            DELETE FROM public.retention_purge_jobs job
            WHERE job.root_type = 'knowledge_item' AND job.root_id = target_knowledge;
        END IF;
    END IF;

    target_revision := NULL;
    SELECT document.id INTO target_document
    FROM public.retention_purge_jobs job
    JOIN public.documents document ON document.id = job.root_id
    WHERE job.root_type = 'document' AND document.archived_at <= archived_before
    ORDER BY job.archived_at, job.root_id
    FOR UPDATE OF job, document SKIP LOCKED
    LIMIT 1;
    IF target_document IS NOT NULL THEN
        UPDATE public.documents
        SET current_revision_id = NULL
        WHERE id = target_document AND current_revision_id IS NOT NULL;
    END IF;

    SELECT revision.id INTO target_revision
    FROM public.document_revisions revision
    JOIN public.documents document ON document.id = revision.document_id
    WHERE ((
        target_document IS NOT NULL AND revision.document_id = target_document
    ) OR (
        revision.created_at <= revision_before
        AND revision.id IS DISTINCT FROM document.current_revision_id
    ))
      AND NOT EXISTS (
          SELECT 1
          FROM public.provenance_links link
          LEFT JOIN public.document_revision_chunks chunk
            ON chunk.id = link.document_revision_chunk_id
          WHERE link.document_revision_id = revision.id
             OR chunk.document_revision_id = revision.id
      )
    ORDER BY (revision.document_id = target_document) DESC, revision.created_at, revision.id
    FOR UPDATE OF revision SKIP LOCKED
    LIMIT 1;
    IF target_revision IS NOT NULL THEN
        RETURN QUERY
        WITH candidates AS MATERIALIZED (
            SELECT outbox.id FROM public.job_outbox outbox
            JOIN public.ingestion_jobs job ON job.id = outbox.job_id
            WHERE job.document_revision_id = target_revision
            ORDER BY outbox.id LIMIT max_rows
        ), deleted AS (
            DELETE FROM public.job_outbox outbox USING candidates candidate
            WHERE outbox.id = candidate.id RETURNING outbox.id
        )
        SELECT 'job_outbox'::text, deleted.id::text, NULL::text FROM deleted;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;

        RETURN QUERY
        WITH candidates AS MATERIALIZED (
            SELECT unit.id FROM public.retrieval_units unit
            JOIN public.document_revision_chunks chunk
              ON chunk.id = unit.document_revision_chunk_id
            WHERE chunk.document_revision_id = target_revision
            ORDER BY unit.id LIMIT max_rows
        ), deleted AS (
            DELETE FROM public.retrieval_units unit USING candidates candidate
            WHERE unit.id = candidate.id RETURNING unit.id
        )
        SELECT 'retrieval_unit'::text, deleted.id::text, NULL::text FROM deleted;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;

        RETURN QUERY
        WITH candidates AS MATERIALIZED (
            SELECT job.id FROM public.ingestion_jobs job
            WHERE job.document_revision_id = target_revision
              AND NOT EXISTS (
                  SELECT 1 FROM public.job_outbox outbox WHERE outbox.job_id = job.id
              )
            ORDER BY job.id LIMIT max_rows
        ), deleted AS (
            DELETE FROM public.ingestion_jobs job USING candidates candidate
            WHERE job.id = candidate.id RETURNING job.id
        )
        SELECT 'ingestion_job'::text, deleted.id::text, NULL::text FROM deleted;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;

        RETURN QUERY
        WITH candidates AS MATERIALIZED (
            SELECT chunk.id FROM public.document_revision_chunks chunk
            WHERE chunk.document_revision_id = target_revision
              AND NOT EXISTS (
                  SELECT 1 FROM public.retrieval_units unit
                  WHERE unit.document_revision_chunk_id = chunk.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM public.provenance_links link
                  WHERE link.document_revision_chunk_id = chunk.id
              )
            ORDER BY chunk.id LIMIT max_rows
        ), deleted AS (
            DELETE FROM public.document_revision_chunks chunk USING candidates candidate
            WHERE chunk.id = candidate.id RETURNING chunk.id
        )
        SELECT 'document_revision_chunk'::text, deleted.id::text, NULL::text FROM deleted;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;

        RETURN QUERY
        DELETE FROM public.document_revisions revision
        WHERE revision.id = target_revision
          AND NOT EXISTS (
              SELECT 1 FROM public.document_revision_chunks chunk
              WHERE chunk.document_revision_id = revision.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM public.ingestion_jobs job
              WHERE job.document_revision_id = revision.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM public.provenance_links link
              WHERE link.document_revision_id = revision.id
          )
        RETURNING 'document_revision'::text, revision.id::text, revision.storage_key;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;
    END IF;

    IF target_document IS NOT NULL THEN
        RETURN QUERY
        DELETE FROM public.documents document
        WHERE document.id = target_document
          AND document.current_revision_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM public.document_revisions revision
              WHERE revision.document_id = document.id
          )
        RETURNING 'document'::text, document.id::text, NULL::text;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;
        IF affected > 0 THEN
            DELETE FROM public.retention_purge_jobs job
            WHERE job.root_type = 'document' AND job.root_id = target_document;
        END IF;
    END IF;

    RETURN QUERY
    WITH candidates AS MATERIALIZED (
        SELECT record.id FROM public.idempotency_records record
        WHERE record.expires_at <= operational_before
        ORDER BY record.expires_at, record.id FOR UPDATE SKIP LOCKED LIMIT max_rows
    ), deleted AS (
        DELETE FROM public.idempotency_records record USING candidates candidate
        WHERE record.id = candidate.id RETURNING record.id
    )
    SELECT 'idempotency_record'::text, deleted.id::text, NULL::text FROM deleted;
    GET DIAGNOSTICS affected = ROW_COUNT;
    total_affected := total_affected + affected;

    RETURN QUERY
    WITH candidates AS MATERIALIZED (
        SELECT action.id FROM public.pending_ai_actions action
        WHERE action.expires_at <= operational_before
        ORDER BY action.expires_at, action.id FOR UPDATE SKIP LOCKED LIMIT max_rows
    ), deleted AS (
        DELETE FROM public.pending_ai_actions action USING candidates candidate
        WHERE action.id = candidate.id RETURNING action.id
    )
    SELECT 'pending_ai_action'::text, deleted.id::text, NULL::text FROM deleted;
    GET DIAGNOSTICS affected = ROW_COUNT;
    total_affected := total_affected + affected;

    SELECT family.id INTO target_family
    FROM public.session_families family
    WHERE COALESCE(family.revoked_at, family.absolute_expires_at) <= operational_before
    ORDER BY COALESCE(family.revoked_at, family.absolute_expires_at), family.id
    FOR UPDATE SKIP LOCKED LIMIT 1;
    IF target_family IS NOT NULL THEN
        RETURN QUERY
        WITH candidates AS MATERIALIZED (
            SELECT credential.id FROM public.session_credentials credential
            WHERE credential.family_id = target_family
            ORDER BY credential.id LIMIT max_rows
        ), deleted AS (
            DELETE FROM public.session_credentials credential USING candidates candidate
            WHERE credential.id = candidate.id RETURNING credential.id
        )
        SELECT 'session_credential'::text, deleted.id::text, NULL::text FROM deleted;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;

        RETURN QUERY
        DELETE FROM public.session_families family
        WHERE family.id = target_family
          AND NOT EXISTS (
              SELECT 1 FROM public.session_credentials credential
              WHERE credential.family_id = family.id
          )
        RETURNING 'session_family'::text, family.id::text, NULL::text;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;
    END IF;

    SELECT key.id INTO target_key
    FROM public.personal_api_keys key
    WHERE COALESCE(key.revoked_at, key.expires_at) <= operational_before
    ORDER BY COALESCE(key.revoked_at, key.expires_at), key.id
    FOR UPDATE SKIP LOCKED LIMIT 1;
    IF target_key IS NOT NULL THEN
        RETURN QUERY
        WITH candidates AS MATERIALIZED (
            SELECT scope.api_key_id, scope.scope FROM public.api_key_scopes scope
            WHERE scope.api_key_id = target_key
            ORDER BY scope.scope LIMIT max_rows
        ), deleted AS (
            DELETE FROM public.api_key_scopes scope USING candidates candidate
            WHERE scope.api_key_id = candidate.api_key_id
              AND scope.scope = candidate.scope
            RETURNING scope.api_key_id, scope.scope
        )
        SELECT 'api_key_scope'::text,
               deleted.api_key_id::text || ':' || deleted.scope,
               NULL::text
        FROM deleted;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;

        RETURN QUERY
        DELETE FROM public.personal_api_keys key
        WHERE key.id = target_key
          AND NOT EXISTS (
              SELECT 1 FROM public.api_key_scopes scope
              WHERE scope.api_key_id = key.id
          )
        RETURNING 'personal_api_key'::text, key.id::text, NULL::text;
        GET DIAGNOSTICS affected = ROW_COUNT;
        total_affected := total_affected + affected;
    END IF;

    RETURN QUERY
    WITH candidates AS MATERIALIZED (
        SELECT bucket.id FROM public.login_throttle_buckets bucket
        WHERE bucket.updated_at <= operational_before
          AND (bucket.blocked_until IS NULL OR bucket.blocked_until <= operational_before)
        ORDER BY bucket.updated_at, bucket.id FOR UPDATE SKIP LOCKED LIMIT max_rows
    ), deleted AS (
        DELETE FROM public.login_throttle_buckets bucket USING candidates candidate
        WHERE bucket.id = candidate.id RETURNING bucket.id
    )
    SELECT 'login_throttle_bucket'::text, deleted.id::text, NULL::text FROM deleted;
    GET DIAGNOSTICS affected = ROW_COUNT;
    total_affected := total_affected + affected;

    IF total_affected > 0 THEN
        INSERT INTO public.audit_events (
            id, actor_member_id, actor_kind, request_id, action,
            resource_type, resource_id, outcome, details
        ) VALUES (
            gen_random_uuid(), NULL, 'system', 'retention:' || txid_current()::text,
            'maintenance.retention.purge', 'retention_batch', NULL, 'success',
            jsonb_build_object('purged_records', total_affected, 'per_type_work_limit', max_rows)
        );
    END IF;
END
$$
"""


PROVENANCE_GUARD_FUNCTION = """
CREATE FUNCTION gateway_reject_archived_document_provenance()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    source_document uuid;
    source_archived boolean;
BEGIN
    IF NEW.document_revision_id IS NOT NULL THEN
        SELECT document.id, document.archived_at IS NOT NULL
        INTO source_document, source_archived
        FROM public.document_revisions revision
        JOIN public.documents document ON document.id = revision.document_id
        WHERE revision.id = NEW.document_revision_id
        FOR UPDATE OF document;
    ELSIF NEW.document_revision_chunk_id IS NOT NULL THEN
        SELECT document.id, document.archived_at IS NOT NULL
        INTO source_document, source_archived
        FROM public.document_revision_chunks chunk
        JOIN public.documents document ON document.id = chunk.document_id
        WHERE chunk.id = NEW.document_revision_chunk_id
        FOR UPDATE OF document;
    END IF;
    IF source_document IS NOT NULL AND source_archived THEN
        RAISE EXCEPTION 'Archived documents cannot receive new provenance links';
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.execute("SELECT pg_advisory_xact_lock(739814621451062319)")
    op.execute(
        "DO $$ DECLARE role_contract record; BEGIN "
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolreplication, rolbypassrls "
        "INTO role_contract FROM pg_roles WHERE rolname = 'gateway_maintenance'; "
        "IF NOT FOUND THEN "
        "CREATE ROLE gateway_maintenance NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION BYPASSRLS; "
        "ELSIF role_contract.rolcanlogin OR role_contract.rolsuper OR role_contract.rolcreatedb "
        "OR role_contract.rolcreaterole OR role_contract.rolinherit OR role_contract.rolreplication "
        "OR NOT role_contract.rolbypassrls THEN "
        "RAISE EXCEPTION 'gateway_maintenance role violates the retention contract'; "
        "END IF; END $$"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO gateway_maintenance")
    op.create_table(
        "retention_purge_jobs",
        sa.Column("root_type", sa.String(length=32), nullable=False),
        sa.Column("root_id", sa.UUID(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("root_type IN ('knowledge_item','document')", name="ck_retention_purge_jobs_root_type"),
        sa.PrimaryKeyConstraint("root_type", "root_id"),
    )
    op.create_index(
        "ix_retention_purge_jobs_order",
        "retention_purge_jobs",
        ["root_type", "archived_at", "root_id"],
    )
    op.execute("REVOKE ALL ON retention_purge_jobs FROM PUBLIC")
    op.execute(
        "GRANT SELECT ON api_key_scopes, document_revision_chunks, document_revisions, documents, "
        "idempotency_records, ingestion_jobs, job_outbox, knowledge_items, knowledge_revisions, "
        "login_throttle_buckets, pending_ai_actions, personal_api_keys, provenance_links, "
        "retention_purge_jobs, retrieval_units, session_credentials, session_families TO gateway_maintenance"
    )
    op.execute(
        "GRANT UPDATE ON document_revisions, documents, idempotency_records, knowledge_items, "
        "knowledge_revisions, login_throttle_buckets, pending_ai_actions, personal_api_keys, "
        "retention_purge_jobs, session_families TO gateway_maintenance"
    )
    op.execute(
        "GRANT DELETE ON document_revisions, documents, idempotency_records, knowledge_items, "
        "knowledge_revisions, login_throttle_buckets, pending_ai_actions, personal_api_keys, "
        "provenance_links, retention_purge_jobs, retrieval_units, session_credentials, "
        "session_families, api_key_scopes, document_revision_chunks, ingestion_jobs, "
        "job_outbox TO gateway_maintenance"
    )
    op.execute("GRANT INSERT ON audit_events, retention_purge_jobs TO gateway_maintenance")
    op.create_index("ix_knowledge_items_retention_archived", "knowledge_items", ["archived_at", "id"], postgresql_where=sa.text("archived_at IS NOT NULL"))
    op.create_index("ix_documents_retention_archived", "documents", ["archived_at", "id"], postgresql_where=sa.text("archived_at IS NOT NULL"))
    op.create_index("ix_knowledge_revisions_retention_created", "knowledge_revisions", ["created_at", "id"])
    op.create_index("ix_document_revisions_retention_created", "document_revisions", ["created_at", "id"])
    op.create_index("ix_idempotency_records_retention_expiry", "idempotency_records", ["expires_at", "id"])
    op.create_index("ix_pending_ai_actions_retention_expiry", "pending_ai_actions", ["expires_at", "id"])
    op.create_index("ix_login_throttle_buckets_retention_updated", "login_throttle_buckets", ["updated_at", "id"])
    op.execute("CREATE INDEX ix_session_families_retention_expiry ON session_families (COALESCE(revoked_at, absolute_expires_at), id)")
    op.execute("CREATE INDEX ix_personal_api_keys_retention_expiry ON personal_api_keys (COALESCE(revoked_at, expires_at), id)")
    op.execute(RETENTION_FUNCTION)
    op.execute(PROVENANCE_GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_provenance_reject_archived_document "
        "BEFORE INSERT OR UPDATE ON provenance_links "
        "FOR EACH ROW EXECUTE FUNCTION gateway_reject_archived_document_provenance()"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION gateway_run_retention(timestamptz, timestamptz, timestamptz, integer) FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION gateway_reject_archived_document_provenance() FROM PUBLIC"
    )
    op.execute("GRANT gateway_maintenance TO CURRENT_USER")
    op.execute("GRANT CREATE ON SCHEMA public TO gateway_maintenance")
    op.execute(
        "ALTER FUNCTION gateway_run_retention(timestamptz, timestamptz, timestamptz, integer) OWNER TO gateway_maintenance"
    )
    op.execute(
        "ALTER FUNCTION gateway_reject_archived_document_provenance() OWNER TO gateway_maintenance"
    )
    op.execute("REVOKE CREATE ON SCHEMA public FROM gateway_maintenance")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_provenance_reject_archived_document ON provenance_links")
    op.execute("SET LOCAL ROLE gateway_maintenance")
    op.execute("DROP FUNCTION IF EXISTS gateway_reject_archived_document_provenance()")
    op.execute("DROP FUNCTION IF EXISTS gateway_run_retention(timestamptz, timestamptz, timestamptz, integer)")
    op.execute("RESET ROLE")
    op.execute("DROP INDEX IF EXISTS ix_personal_api_keys_retention_expiry")
    op.execute("DROP INDEX IF EXISTS ix_session_families_retention_expiry")
    op.drop_index("ix_login_throttle_buckets_retention_updated", table_name="login_throttle_buckets")
    op.drop_index("ix_pending_ai_actions_retention_expiry", table_name="pending_ai_actions")
    op.drop_index("ix_idempotency_records_retention_expiry", table_name="idempotency_records")
    op.drop_index("ix_document_revisions_retention_created", table_name="document_revisions")
    op.drop_index("ix_knowledge_revisions_retention_created", table_name="knowledge_revisions")
    op.drop_index("ix_documents_retention_archived", table_name="documents")
    op.drop_index("ix_knowledge_items_retention_archived", table_name="knowledge_items")
    op.execute("REVOKE INSERT ON audit_events, retention_purge_jobs FROM gateway_maintenance")
    op.execute(
        "REVOKE DELETE ON document_revisions, documents, idempotency_records, knowledge_items, "
        "knowledge_revisions, login_throttle_buckets, pending_ai_actions, personal_api_keys, "
        "provenance_links, retention_purge_jobs, retrieval_units, session_credentials, "
        "session_families, api_key_scopes, document_revision_chunks, ingestion_jobs, "
        "job_outbox FROM gateway_maintenance"
    )
    op.execute(
        "REVOKE UPDATE ON document_revisions, documents, idempotency_records, knowledge_items, "
        "knowledge_revisions, login_throttle_buckets, pending_ai_actions, personal_api_keys, "
        "retention_purge_jobs, session_families FROM gateway_maintenance"
    )
    op.execute(
        "REVOKE SELECT ON api_key_scopes, document_revision_chunks, document_revisions, documents, "
        "idempotency_records, ingestion_jobs, job_outbox, knowledge_items, knowledge_revisions, "
        "login_throttle_buckets, pending_ai_actions, personal_api_keys, provenance_links, "
        "retention_purge_jobs, retrieval_units, session_credentials, session_families FROM gateway_maintenance"
    )
    op.drop_index("ix_retention_purge_jobs_order", table_name="retention_purge_jobs")
    op.drop_table("retention_purge_jobs")
