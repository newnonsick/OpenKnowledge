# Troubleshooting

Diagnosis guidance for problems that can reasonably arise from this repository's setup. Each entry states the symptom, the likely cause, and the fix grounded in the implementation.

## Startup and configuration

### Production startup fails with a `RuntimeSafety` validation error

`validate_runtime_safety` refuses unsafe production settings. The error message names the offending field. Common triggers: wildcard `CORS_ORIGINS`, missing or non-HTTPS `PUBLIC_BASE_URL`, wildcard or empty `TRUSTED_HOSTS`, `LOG_LEVEL=DEBUG`, legacy static keys enabled, an `API_KEY_PEPPERS` entry shorter than 32 bytes, or an invalid `MFA_ENCRYPTION_KEYS` entry. Correct the value in `.env.production` and start again. The full rule list is in [configuration.md](configuration.md).

### The worker exits immediately

`src/gateway/worker.py` refuses to start in four situations, each with an explicit error:

- `WORKER_DATABASE_URL is required`: the variable is unset.
- `Worker and web runtime database roles must use distinct URLs`: the worker URL uses the same database role as `DATABASE_URL`.
- `Worker database schema is incompatible`: apply migrations before starting the worker.
- Role validation failure: the `gateway_worker` privileges do not match `deploy/grant-runtime.sql` (typically the permissions one-shot service did not run after a migration).

### `/healthz/ready` returns 503 with `schema_incompatible`

The application started against a database whose Alembic revision or embedding dimension does not match. Run:

```bash
python -m src.gateway.cli current   # inspect
python -m src.gateway.cli migrate   # apply
python -m src.gateway.cli check     # verify
```

The same check runs against `EMBEDDING_DIMENSION`; the schema is fixed at 1024, so a changed value must be reverted rather than migrated.

### Gateway cannot reach PostgreSQL

Confirm the URL scheme is `postgresql+asyncpg://`, that pgvector is installed in the cluster, and that the role exists. In Compose, roles are created only on first database initialization by `deploy/postgres-init.sh`; if the data volume predates a role change, recreate the volume or create the roles manually.

### Requests return 400 immediately after deployment

`TrustedHostMiddleware` rejects `Host` headers outside `TRUSTED_HOSTS`. Accessing the deployment by raw IP address, or by a name not in the list, produces this failure. Add the name to `TRUSTED_HOSTS`, and when behind the Compose edge add the proxy subnet to `TRUSTED_PROXY_CIDRS` so client IP throttling works.

## Authentication and sessions

### Login returns 429

The login throttle engaged: five failures per account in 15 minutes, twenty per IP, or two hundred globally. Wait for the retry window named in the response or clear the bucket in `login_throttle_buckets` during development.

### Browser sessions never establish; console redirects to `/login` in a loop

Session cookies use the `__Host-` and `__Secure-` prefixes, which browsers only store over HTTPS (localhost is exempt). Serving the console over plain HTTP prevents cookie storage. Terminate TLS at the edge as the Compose topology does, or develop on `localhost`.

### Refresh fails with a CSRF error

The refresh endpoint compares the `Origin` header against `PUBLIC_BASE_URL`. A mismatch, for example a trailing-slash difference or accessing through an alternate domain, fails verification. Ensure `PUBLIC_BASE_URL` exactly matches the origin the browser uses.

### Super admin login always fails

Super admins must present a TOTP code or a recovery code with their password. If the authenticator is lost, use a recovery code, or reset the account with `python -m src.gateway.cli recover-super-admin --username <name>` from the migration environment.

### API key authentication returns 401 despite a valid-looking key

Personal API keys are shown once; only peppered hashes are stored. Recreating the same key name does not reproduce the secret. If `API_KEY_PEPPERS` changed since the key was created, the hash can no longer be verified; issue a new key. Also confirm the key is sent as `Authorization: Bearer ...` or `x-api-key`, not both.

## Data and ingestion

### Uploaded sources stay in `queued`

Ingestion only progresses when a worker is running. Verify the worker process or Compose service is up and its readiness prerequisites passed. Inspect job state, attempts, and errors through `/api/v1/ingestion-jobs`.

### An ingestion job is stuck in a running state after a worker crash

Jobs are claimed under a lease (`WORKER_LEASE_SECONDS`, default 60). A crashed worker's lease expires and another worker reclaims the job. For an immediate retry or cancellation, use the job's `retry` or `cancel` endpoints from the console.

### Ingestion fails with parser limit errors

Parsing is bounded by `PARSER_TIMEOUT_SECONDS`, `PARSER_MEMORY_LIMIT_BYTES`, `PARSER_CPU_SECONDS`, `PARSER_MAX_PAGES`, `PARSER_MAX_OUTPUT_CHARACTERS`, and `INGESTION_MAX_CHUNKS`. Documents exceeding a limit fail the job with the corresponding failure code; split the document or raise the relevant setting.

### Knowledge updates return a conflict

Updates and deletes require `expected_version`. A conflict means another writer appended a newer revision. Re-read the item, merge changes, and retry with the current version. This is the intended optimistic-concurrency behavior.

## Testing and build

### Integration tests silently pass on SQLite

Without `TEST_DATABASE_URL` (or `DATABASE_URL` in `.env`), unit-oriented tests fall back to SQLite. That fallback cannot exercise PostgreSQL full-text search, pgvector, HNSW, transactions, or migration release flow. Export `TEST_DATABASE_URL` to run the real integration profile.

### CI fails on the generated OpenAPI client

The web job regenerates `web/lib/generated/openapi.ts` from the committed `openapi.json` and fails on drift. After changing an HTTP schema, re-export and regenerate, then commit both files together:

```powershell
uv run --no-sync --env-file .env python -m scripts.export_openapi --output web/lib/generated/openapi.json
Set-Location web; npm run generate:api
```

### `pip install --require-hashes` fails

Hash-pinned installs fail when mixing lockfiles with ad-hoc requirements. Install exactly `requirements/runtime.lock` or `requirements/dev.lock`. To change dependencies, edit `pyproject.toml` and regenerate the locks with pip-tools as described in `requirements/README.md`.

### Restore refuses to run

`scripts/restore.sh` requires an isolated target: `RESTORE_CONFIRM_ISOLATED=true`, a newly created empty database whose name matches `RESTORE_EXPECTED_DATABASE`, and an existing, writable, empty `RESTORE_STORAGE_DIR`. Any violation aborts before writing. These guards exist to prevent restoring over live data; never bypass them.

## Getting more information

- Structured JSON logs carry `request_id`; the same id appears in API error responses and can be used to correlate.
- `/metrics` exposes request counters, latency, and authentication events without authentication.
- `/api/v1/operations/summary` and the console dashboard surface job and ingestion state.
- The audit trail (`/api/v1/audit-events`) records who changed what for security-relevant actions.
