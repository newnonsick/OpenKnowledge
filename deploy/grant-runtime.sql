\set ON_ERROR_STOP on
GRANT CONNECT ON DATABASE :DBNAME TO gateway_runtime, gateway_worker;
GRANT USAGE ON SCHEMA public TO gateway_runtime, gateway_worker;
GRANT SELECT ON alembic_version TO gateway_worker;
GRANT SELECT ON alembic_version, api_key_scopes, audit_events, compatibility_principals, document_chunks, document_files, document_revision_chunks, document_revisions, documents, embedding_generations, idempotency_records, ingestion_jobs, knowledge_items, knowledge_revisions, login_throttle_buckets, members, mfa_factors, mfa_recovery_codes, password_credentials, pending_ai_actions, personal_api_keys, provenance_links, retrieval_units, runtime_setting_revisions, session_credentials, session_families, space_memberships, workspaces TO gateway_runtime;
GRANT INSERT ON api_key_scopes, audit_events, document_chunks, document_files, document_revisions, documents, idempotency_records, ingestion_jobs, job_outbox, knowledge_items, knowledge_revisions, login_throttle_buckets, members, mfa_factors, mfa_recovery_codes, password_credentials, pending_ai_actions, personal_api_keys, provenance_links, retrieval_units, runtime_setting_revisions, session_credentials, session_families, space_memberships, workspaces TO gateway_runtime;
GRANT UPDATE ON document_chunks, document_files, idempotency_records, knowledge_items, login_throttle_buckets, members, mfa_factors, mfa_recovery_codes, password_credentials, personal_api_keys, session_credentials, session_families TO gateway_runtime;
GRANT DELETE ON api_key_scopes, document_chunks, document_files, knowledge_items, space_memberships TO gateway_runtime;
GRANT UPDATE (revoked_at) ON compatibility_principals TO gateway_runtime;
GRANT UPDATE (display_name, current_revision_id, archived_at, revision, updated_at) ON documents TO gateway_runtime;
GRANT UPDATE (staging_storage_key, storage_key) ON document_revisions TO gateway_runtime;
GRANT UPDATE (state, cancellation_requested, retry_requested, updated_at) ON ingestion_jobs TO gateway_runtime;
GRANT UPDATE (state, confirmed_by_member_id, confirmed_at, consumed_at) ON pending_ai_actions TO gateway_runtime;
GRANT UPDATE (active, deactivated_at) ON retrieval_units TO gateway_runtime;
GRANT UPDATE (state, activation_reason, activated_by_member_id, activated_at) ON runtime_setting_revisions TO gateway_runtime;
GRANT UPDATE (role, updated_at) ON space_memberships TO gateway_runtime;
GRANT UPDATE (name, archived_at, revision) ON workspaces TO gateway_runtime;
GRANT USAGE, SELECT ON login_throttle_buckets_id_seq TO gateway_runtime;
GRANT EXECUTE ON FUNCTION gateway_actor_active(), gateway_has_space_role(text, text[]), gateway_is_initial_space_owner(text, uuid), gateway_can_change_membership(text, uuid, text), gateway_actor_super_admin() TO gateway_runtime;
GRANT SELECT ON document_revision_chunks, document_revisions, documents, embedding_generations, ingestion_jobs, job_outbox, members, operational_alerts, retrieval_units, space_memberships, workspaces TO gateway_worker;
GRANT INSERT ON document_revision_chunks, job_outbox, operational_alerts, retrieval_units TO gateway_worker;
GRANT UPDATE (current_revision_id, revision, updated_at) ON documents TO gateway_worker;
GRANT UPDATE (staging_storage_key, parser_version, status, failure_code, ready_at, activated_at) ON document_revisions TO gateway_worker;
GRANT UPDATE (state, progress, attempt_count, next_attempt_at, cancellation_requested, retry_requested, last_error_code, last_error_detail, lease_owner, lease_expires_at, claim_token, updated_at, started_at, finished_at) ON ingestion_jobs TO gateway_worker;
GRANT UPDATE (state, attempt_count, last_error_code, lease_owner, lease_expires_at, claim_token, available_at, published_at, updated_at) ON job_outbox TO gateway_worker;
GRANT UPDATE (active, deactivated_at) ON retrieval_units TO gateway_worker;
DO $$
DECLARE
    retained_membership boolean := pg_has_role(current_user, 'gateway_maintenance', 'MEMBER');
BEGIN
    IF NOT retained_membership THEN
        EXECUTE format('GRANT gateway_maintenance TO %I', current_user);
    END IF;
    EXECUTE 'ALTER FUNCTION gateway_run_retention(timestamptz, timestamptz, timestamptz, integer) OWNER TO gateway_maintenance';
    EXECUTE 'ALTER FUNCTION gateway_reject_archived_document_provenance() OWNER TO gateway_maintenance';
    EXECUTE 'GRANT EXECUTE ON FUNCTION gateway_run_retention(timestamptz, timestamptz, timestamptz, integer) TO gateway_worker';
    IF NOT retained_membership THEN
        EXECUTE format('REVOKE gateway_maintenance FROM %I', current_user);
    END IF;
END
$$;
