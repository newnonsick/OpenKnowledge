# Database

The system uses one PostgreSQL database with the pgvector extension as its only persistent store. This document covers the schema areas, the migration mechanism, the separated database roles, and retention.

## Schema areas

The schema is versioned through Alembic migrations `001` through `018` under `alembic/versions/`. The tables, as enumerated by the privilege grants in `deploy/grant-runtime.sql`, group into these areas:

| Area | Tables | Purpose |
|---|---|---|
| Knowledge | `knowledge_items`, `knowledge_revisions` | Items with immutable, hash-chained revisions |
| Documents and ingestion | `documents`, `document_revisions`, `document_revision_chunks`, `document_chunks`, `document_files`, `retrieval_units`, `embedding_generations`, `ingestion_jobs`, `job_outbox` | Durable source ingestion pipeline, from upload through activation |
| Identity | `members`, `password_credentials`, `mfa_factors`, `mfa_recovery_codes`, `session_families`, `session_credentials`, `personal_api_keys`, `api_key_scopes`, `compatibility_principals` | Accounts, MFA, session families, personal API keys, legacy principals |
| Authorization | `workspaces` (spaces), `space_memberships` | Spaces and per-member roles |
| Operations | `audit_events`, `runtime_setting_revisions`, `idempotency_records`, `login_throttle_buckets`, `pending_ai_actions`, `operational_alerts` | Audit trail, runtime settings, idempotency, throttling, confirmed AI actions |
| Migration bookkeeping | `alembic_version` | Current schema revision |

Search structures:

- Lexical search uses generated `tsvector` columns with GIN indexes, ranked by `ts_rank_cd`. Migration 015 maintains multilingual index variants.
- Vector search uses a pgvector column with cosine distance over HNSW indexes. The dimension is fixed by the current schema at 1024, and readiness fails closed when `EMBEDDING_DIMENSION` disagrees.

## Database roles

The deployment separates database privileges by function. `deploy/postgres-init.sh` creates the roles at first initialization, and `deploy/grant-runtime.sql` applies least-privilege grants after migrations:

| Role | Attributes | Used by | Privileges |
|---|---|---|---|
| `gateway_runtime` | `NOBYPASSRLS`, no superuser | Gateway API | Row-level selects and the specific inserts, updates, and deletes the runtime performs; column-restricted updates on operational tables |
| `gateway_worker` | `BYPASSRLS`, no superuser | Background worker | Lease-scoped updates on `ingestion_jobs` and `job_outbox`, revision and retrieval-unit writes, retention function execution |
| `gateway_maintenance` | `NOLOGIN` | Owner of the retention functions | Holds `gateway_run_retention` and `gateway_reject_archived_document_provenance` so their privileges cannot be changed by the runtime roles |
| `postgres` | Cluster admin | Migrations, permissions service, backup | Full access; used only by the one-shot migrate and permissions services and the backup tooling |

The gateway verifies at production startup that its runtime role's privileges match expectations, and the worker performs the same check for its role before claiming jobs. Row-level security policies (migration 007) restrict runtime queries to spaces the current principal is authorized for, which is why the runtime role does not need and does not have `BYPASSRLS`.

Authorization helper functions (`gateway_actor_active`, `gateway_has_space_role`, `gateway_actor_super_admin`, and related) back the RLS policies and are granted to the runtime role.

## Migrations

Migrations are a deployment step and never run during web or worker startup.

```bash
python -m src.gateway.cli migrate   # apply all revisions
python -m src.gateway.cli current   # print the current revision
python -m src.gateway.cli check     # exit 0 if compatible, 1 otherwise
```

Behavior details:

- The programmatic runner lives in `src/gateway/infrastructure/migrations.py` and uses `MIGRATION_DATABASE_URL` when set, otherwise `DATABASE_URL`.
- Web and worker startup perform a read-only revision check. On mismatch the gateway keeps serving liveness but readiness fails with `schema_incompatible`, and the worker refuses to start.
- In Compose, a one-shot `migrate` service runs before the `permissions` service applies grants; the application services start only after both complete.
- Migration procedures in production (backup, quiesce writers, migrate, verify readiness, restart) are described in [operations.md](operations.md).

Alembic itself is configured by `alembic.ini` with `script_location = alembic`; the application uses the programmatic runner rather than the `alembic` CLI, and tests verify migration determinism and release flow (`tests/unit/test_migration_determinism.py`, `tests/integration/test_migration_release_flow.py`).

## Retention

Retention removes three classes of data, each with an enforced minimum age: archived knowledge and unreferenced archived documents (at least 365 days), superseded knowledge revisions (at least 1095 days), and expired operational records (at least 30 days). The policy is deliberately conservative:

- The runner is disabled unless `RETENTION_PURGE_ENABLED=true`.
- All deletion goes through the `gateway_run_retention` function, which independently rejects cutoffs shorter than the minima even if the worker process is compromised.
- Work proceeds in bounded batches per daily cycle; large revision subtrees are removed in bounded child batches so maximum-size documents cannot strand objects.
- Documents that still provide provenance for retained knowledge are not purged.
- Every batch commits with a system audit event before any immutable file is removed; failed file removals become safe unreferenced objects that the storage reconciler cleans up later.

Operational guidance, including the requirement to rehearse a restore before enabling retention, is in [operations.md](operations.md).

## Backup and restore

Backups are coordinated, encrypted bundles produced by `scripts/backup.sh`:

- Writers must be stopped and `BACKUP_WRITERS_QUIESCED=true` must be set; the script refuses otherwise.
- The backup role must be a superuser or hold `BYPASSRLS`; row-filtered roles are rejected.
- A bundle contains a `pg_dump` custom-format database dump, the object storage tree, a database-bound storage manifest, cutoff metadata with the Alembic revision, and SHA-256 checksums, encrypted with age.

`scripts/restore.sh` restores a bundle into a newly created empty database and an existing empty storage directory, verifying checksums, manifest consistency, and isolation before writing. The restore drills and monthly rehearsals that validate the procedure are described in [operations.md](operations.md) and [deployment.md](deployment.md).

## Test database handling

Integration tests create and drop a guarded unique schema per test lifecycle inside the target database. The PostgreSQL URL is provided by:

1. `TEST_DATABASE_URL` if set, or
2. the `DATABASE_URL` from `.env` loaded by the test environment loader (`tests/e2e/harness/test_env.py`).

Without a PostgreSQL URL, unit tests fall back to SQLite with PostgreSQL-specific types compiled for fast checks. The SQLite fallback cannot exercise full-text search, pgvector operators, HNSW indexes, transaction behavior, or the Alembic release flow; capability claims about those require the PostgreSQL profile.
