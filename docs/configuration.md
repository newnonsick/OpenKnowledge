# Configuration

All configuration loads from environment variables and an optional `.env` file at the repository root (loaded by pydantic-settings with `extra = "ignore"`). Two templates ship with the repository:

- `.env.example`: development template.
- `.env.production.example`: template for the Compose production stack, including the Compose-level database and domain variables.

Settings are declared in `src/gateway/config.py`. Nested settings objects (`gateway`, `database`, `llm`, `embedding`) each read the same environment; aliases exist for lowercase variants but the uppercase names below are canonical.

## Gateway server

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `development` | Runtime profile: `development`, `test`, or `production`. Production enables additional startup validation. |
| `HOST` | `127.0.0.1` | Bind address. The Docker image and Compose set `0.0.0.0`. |
| `PORT` | `8000` | HTTP port. |
| `LOG_LEVEL` | `DEBUG` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. `DEBUG` is rejected in production. |
| `LOG_FORMAT` | `auto` | `auto`, `console`, or `json`. Automatic output uses readable console records outside production and JSON records in production. |
| `STORAGE_DIR` | `./data/storage` | Directory for uploaded document files. Compose mounts a shared volume at `/data/storage`. |
| `STORAGE_BACKEND` | `local` | Versioned object storage backend: `local` disk or `s3` for any S3-compatible endpoint. Startup fails fast when `s3` is selected without complete credentials. |
| `STORAGE_S3_ENDPOINT` | unset | S3-compatible endpoint URL, for example `https://s3.example.com` or a MinIO/Garage URL. Required when `STORAGE_BACKEND=s3`. |
| `STORAGE_S3_BUCKET` | unset | S3 bucket holding versioned storage objects. Required when `STORAGE_BACKEND=s3`. |
| `STORAGE_S3_REGION` | unset | S3 signing region for the bucket. Required when `STORAGE_BACKEND=s3`. |
| `STORAGE_S3_ACCESS_KEY` | unset | S3 access key id. Required when `STORAGE_BACKEND=s3`. |
| `STORAGE_S3_SECRET_KEY` | unset | S3 secret access key. Required when `STORAGE_BACKEND=s3`. |
| `STORAGE_S3_SESSION_TOKEN` | unset | Optional session token for temporary S3 credentials. |
| `STORAGE_S3_PREFIX` | unset | Optional key prefix inside the bucket. Local and S3 key layouts are otherwise identical (`staging/{space}/{upload}`, `objects/{space}/{doc}/{rev}`). |
| `OIDC_ENABLED` | `false` | Enables OIDC single sign-on for management authentication. Startup fails fast when enabled without complete settings. |
| `OIDC_ISSUER` | unset | OIDC issuer URL used for discovery (`/.well-known/openid-configuration`) and ID-token `iss` verification. |
| `OIDC_CLIENT_ID` | unset | Client identifier registered with the identity provider; also the ID-token `aud`. |
| `OIDC_CLIENT_SECRET` | unset | Client secret used for the authorization-code exchange. |
| `OIDC_REDIRECT_URL` | unset | Public callback URL registered with the provider, ending in `/api/v1/auth/oidc/callback`. |
| `OIDC_GROUP_CLAIM` | `groups` | ID-token claim carrying group memberships. |
| `OIDC_USERNAME_CLAIM` | `email` | ID-token claim used as the member username, falling back to `email`, `preferred_username`, then `sub`. |
| `OIDC_ROLE_MAPPING` | `{}` | JSON map of IdP group name to system role, for example `{"sso-admins":"super_admin"}`. Unmapped groups default to `member`. |
| `OIDC_GROUP_SPACE_MAP` | `{}` | JSON map of IdP group name to space grants, for example `{"team-a":[{"space_id":"research","role":"editor"}]}`. Roles are `owner`, `editor`, or `reader`. |
| `OIDC_DEPROVISION_DISABLE` | `true` | When all mapped grants disappear, remove the mapped memberships and disable the member instead of deleting it. Disabled members are audited as `member.deprovisioned` and their sessions revoked. |
| `DEFAULT_WORKSPACE_ID` | `global` | Space used when a request does not specify one. Created at startup if missing. |
| `PUBLIC_BASE_URL` | unset | Public HTTPS origin. Required in production; the exact origin is allowed for authenticated session mutations. Development falls back to the two local console origins when unset. |
| `TRUSTED_HOSTS` | `localhost,127.0.0.1,[::1],testserver,test,gateway-test` | HTTP `Host` allowlist. Explicit non-wildcard values are required in production. Comma-separated or a JSON array. |
| `TRUSTED_PROXY_CIDRS` | unset | Proxy networks trusted for client IP resolution. Comma-separated CIDRs; each value is validated as an IP network. |
| `CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Allowed browser origins. Wildcard `*` is rejected in production. Comma-separated or a JSON array. |
| `MAX_REQUEST_BODY_BYTES` | `16777216` | Maximum request body size enforced by middleware. |
| `MAX_UPLOAD_BYTES` | `10485760` | Maximum uploaded file size. |
| `KNOWLEDGE_SYSTEM_PROMPT_ENABLED` | `true` | Append the knowledge usage directive to the system prompt. |
| `KNOWLEDGE_SYSTEM_PROMPT_CUSTOM` | unset | Replace the built-in directive with custom text. |

## Authentication and secrets

| Variable | Default | Description |
|---|---|---|
| `API_KEY_PEPPERS` | unset | JSON map of version number to pepper string, for example `{"1":"..."}`. Personal API keys are hashed with the pepper of their version. Production requires at least one pepper of 32 bytes or more. |
| `ACTIVE_API_KEY_PEPPER_VERSION` | `1` | Version used when creating new keys. Must exist in `API_KEY_PEPPERS`. |
| `MFA_ENCRYPTION_KEYS` | unset | JSON map of version number to a base64url-encoded 32-byte Fernet key. Required in production; TOTP secrets are encrypted with it. |
| `ACTIVE_MFA_ENCRYPTION_KEY_VERSION` | `1` | Version used for new encryptions. Must exist in `MFA_ENCRYPTION_KEYS`. |
| `GATEWAY_API_KEYS` | unset | Legacy static API keys for development and test only. Comma-separated or a JSON array. Forbidden in production. |
| `LEGACY_API_KEYS_ENABLED` | `false` | Enables the legacy static key mechanism. Forbidden in production. |

Key rotation procedure (add a new numbered version, redeploy with the new active version, retire the old one once no live record references it) is described in [operations.md](operations.md).

## Database

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/gateway_db` | Async SQLAlchemy URL used by the gateway runtime. In Compose it uses the `gateway_runtime` role. |
| `MIGRATION_DATABASE_URL` | unset | Privileged URL used only by explicit migration commands. Falls back to `DATABASE_URL`. |
| `WORKER_DATABASE_URL` | unset | Dedicated URL used by the background worker. Required for the worker; its role must differ from the runtime role. |
| `DB_POOL_SIZE` | `20` | Connection pool size. |
| `DB_MAX_OVERFLOW` | `10` | Overflow connections beyond the pool. |
| `DB_POOL_TIMEOUT` | `30.0` | Connection acquisition timeout in seconds. |
| `DB_POOL_RECYCLE` | `1800` | Recycle connections after this many seconds. |
| `DB_ECHO` | `false` | Echo SQL statements to the log. Rejected in production. |

## LLM backend

Any OpenAI-compatible chat completion endpoint.

| Variable | Default | Description |
|---|---|---|
| `LLM_URL` | `http://localhost:8888` | Base URL of the endpoint. |
| `LLM_MODEL_ID` | `default` | Backend model identifier. |
| `LLM_FALLBACK_MODEL_IDS` | unset | Comma-separated fallback model IDs used only after the configured default exhausts transient-error retries. |
| `LLM_API_KEY` | `EMPTY` | Backend API key when the endpoint requires one. |
| `CONTEXT_WINDOW` | `8192` | Context window reported through the model registry. |
| `LLM_TIMEOUT_SECONDS` | `120.0` | HTTP timeout for inference. |
| `LLM_RETRY_ATTEMPTS` | `3` | Maximum attempts (1 to 5) for connection errors, 408, 429, and 5xx. |
| `LLM_RETRY_BACKOFF_SECONDS` | `0.2` | Initial exponential backoff delay (0 to 10), bounded. |
| `LLM_MAX_CONCURRENCY` | `20` | Process-wide concurrent request limit (1 to 500). |
| `LLM_BULKHEAD_TIMEOUT_SECONDS` | `1.0` | Maximum wait for a concurrency slot (above 0 to 30). |
| `LLM_CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive failures before the circuit opens (1 to 100). |
| `LLM_CIRCUIT_RECOVERY_SECONDS` | `30.0` | Open-circuit interval before a recovery probe (above 0 to 600). |
| `LLM_TEMPERATURE` | `0.7` | Default sampling temperature. |
| `LLM_MAX_TOKENS` | unset | Default completion token limit. |
| `LLM_EXTRA_HEADERS` | unset | Additional HTTP headers sent to the LLM provider, as a JSON object. A blank or non-JSON value is ignored. |

## Embedding backend

Any OpenAI-compatible embeddings endpoint.

| Variable | Default | Description |
|---|---|---|
| `EMBEDDING_URL` | `http://localhost:7997` | Base URL of the endpoint. |
| `EMBEDDING_MODEL_ID` | `default` | Embedding model identifier. |
| `EMBEDDING_API_KEY` | `EMPTY` | Backend API key when the endpoint requires one. |
| `EMBEDDING_DIMENSION` | `1024` | Vector dimension of the pgvector columns. Changing it requires `python -m src.gateway.cli set-embedding-dimension`; readiness fails closed until the schema matches. |
| `EMBEDDING_BATCH_SIZE` | `32` | Batch size for embedding requests. |
| `EMBEDDING_TIMEOUT_SECONDS` | `30.0` | HTTP timeout for embedding calls. |
| `EMBEDDING_RETRY_ATTEMPTS` | `3` | Maximum attempts (1 to 5) for transient failures. |
| `EMBEDDING_RETRY_BACKOFF_SECONDS` | `0.2` | Initial exponential backoff delay, bounded. |
| `EMBEDDING_MAX_CONCURRENCY` | `20` | Process-wide concurrent request limit. |
| `EMBEDDING_BULKHEAD_TIMEOUT_SECONDS` | `1.0` | Maximum wait for a concurrency slot. |
| `EMBEDDING_CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive failures before the circuit opens. |
| `EMBEDDING_CIRCUIT_RECOVERY_SECONDS` | `30.0` | Open-circuit interval before a recovery probe. |

## Chat orchestration guardrails

| Variable | Default | Description |
|---|---|---|
| `MAX_TOOL_ITERATIONS` | `10` | Maximum model round trips per request. |
| `MAX_INTERNAL_TOOL_CALLS` | `32` | Maximum internal tool executions across one request. |
| `MAX_REPEATED_TOOL_SIGNATURES` | `2` | Maximum executions of one identical internal tool signature. |
| `MAX_TOOL_WALL_CLOCK_SECONDS` | `180.0` | Wall-clock budget for one orchestrated chat request. |
| `TOOL_TIMEOUT_SECONDS` | `15.0` | Timeout for a single internal tool execution. |

## Credential and space budgets

Burst and concurrency guards apply per credential; sustained per-minute budgets are
tracked per credential and space.

| Variable | Default | Description |
|---|---|---|
| `QUOTA_REQUESTS_PER_MINUTE` | `120` | Sustained requests allowed per minute per credential and space; burst guard per credential. |
| `QUOTA_CONCURRENT_REQUESTS` | `8` | Concurrent in-flight requests allowed per credential. |
| `QUOTA_TOKENS_PER_MINUTE` | `60000` | Chat completion tokens allowed per minute per credential and space. |
| `QUOTA_STORAGE_BYTES` | `1073741824` | Stored upload bytes allowed per credential and space. |
| `QUOTA_BURST_REQUESTS` | `20` | Burst requests absorbed before per-credential rate limiting engages. |

## Parsing, chunking, and ingestion

| Variable | Default | Description |
|---|---|---|
| `PARSER_MEMORY_LIMIT_BYTES` | `134217728` | Memory budget for one document parse. |
| `PARSER_TIMEOUT_SECONDS` | `60` | Wall-clock budget for one document parse. |
| `PARSER_CPU_SECONDS` | `30` | CPU-time budget for one document parse. |
| `PARSER_MAX_PAGES` | `500` | Maximum PDF pages parsed. |
| `PARSER_MAX_OUTPUT_CHARACTERS` | `5000000` | Maximum extracted characters per document. |
| `INGESTION_CHUNK_SIZE` | `2000` | Chunk size in characters. |
| `INGESTION_CHUNK_OVERLAP` | `200` | Overlap between consecutive chunks. |
| `INGESTION_MAX_CHUNKS` | `10000` | Maximum chunks per document; ingestion fails beyond this. |

## Worker

| Variable | Default | Description |
|---|---|---|
| `WORKER_LEASE_SECONDS` | `60` | Job claim lease duration. Expired leases can be reclaimed by another worker. |
| `WORKER_HEARTBEAT_SECONDS` | `15` | Lease renewal interval. |
| `WORKER_IDLE_DELAY_SECONDS` | `0.5` | Poll delay when no work is due. |

## Storage maintenance

| Variable | Default | Description |
|---|---|---|
| `STORAGE_RECONCILE_INTERVAL_SECONDS` | `3600` | Interval between storage reconciliation cycles. |
| `STORAGE_STAGING_TTL_SECONDS` | `86400` | Age after which abandoned staging files are expired. |
| `STORAGE_ORPHAN_GRACE_SECONDS` | `86400` | Grace period before unreferenced objects are removed. |

## Retention

Retention executes only when `RETENTION_PURGE_ENABLED` is true, and the database function independently enforces the minima below.

| Variable | Default | Allowed | Description |
|---|---|---|---|
| `RETENTION_PURGE_ENABLED` | `false` | | Master switch for the retention runner. |
| `RETENTION_INTERVAL_SECONDS` | `86400` | at least 300 | Interval between retention cycles. |
| `RETENTION_ARCHIVE_DAYS` | `365` | at least 365 | Minimum age before archived knowledge and unreferenced archived documents are purged. |
| `RETENTION_REVISION_DAYS` | `1095` | at least 1095 | Minimum age before superseded revisions are purged. |
| `RETENTION_OPERATIONAL_DAYS` | `30` | at least 30 | Minimum age before expired operational records are purged. |
| `RETENTION_BATCH_SIZE` | `100` | 1 to 1000 | Rows per database batch. |
| `RETENTION_MAX_BATCHES_PER_CYCLE` | `100` | 1 to 1000 | Batches per cycle; remaining work continues in the next cycle. |
| `WORKER_METRICS_INTERVAL_SECONDS` | `60` | at least 5 | Interval between worker operational-metrics collections. |
| `WORKER_METRICS_FILE` | `/data/storage/metrics/worker.prom` | absolute path or empty | Prometheus textfile the worker publishes its registry to; the gateway merges this file into `/metrics`. Empty disables file publishing and merging. |
| `EMBEDDING_REEMBED_BATCH_SIZE` | `64` | 1 to 512 | Rows re-embedded per batch after an embedding dimension change. |
| `STALE_AFTER_DAYS` | `90` | at least 1 | Age in days after which untouched knowledge is reported as stale by `GET /api/v1/knowledge/stale` and counted in `GET /api/v1/operations/summary`. Detection only; no purging. |

## Compose-level variables

These variables are consumed by `compose.yaml` and the deployment scripts rather than by the application settings.

| Variable | Used by | Description |
|---|---|---|
| `POSTGRES_DB` | Compose | Database name, default `openknowledge`. |
| `POSTGRES_USER` | Compose | PostgreSQL admin user created by the image, default `postgres`. |
| `POSTGRES_PASSWORD` | Compose | PostgreSQL admin password (required). |
| `GATEWAY_RUNTIME_DB_PASSWORD` | Compose | Password of the `gateway_runtime` role created by `deploy/postgres-init.sh` (required). |
| `GATEWAY_WORKER_DB_PASSWORD` | Compose | Password of the `gateway_worker` role (required). |
| `PUBLIC_DOMAIN` | Compose, Caddy | Public DNS name the edge serves with TLS (required). |
| `APP_ENV_FILE` | Compose | Environment file for the gateway and worker services, default `.env.production`. |
| `GATEWAY_INTERNAL_URL` | Web console | Gateway origin the Next.js server proxies `/api/*` to, default `http://127.0.0.1:8000`. |

## Runtime-adjustable settings

Retrieval, chunking, tool, and feature settings can be changed through the management API (`/api/v1/settings` drafts and activation) without editing the environment or restarting. They are versioned with history and rollback. The adjustable groups and their defaults:

- Retrieval: result `limit` 10, `branch_limit` 40, lexical and vector weights 1.0, `rrf_k` 60, minimum lexical score 0.01, minimum vector similarity 0.55, `max_hits_per_source` 2, `active_space_boost` 0.08, `semantic_policy` `prefer`, `hnsw_ef_search` 100, reranker disabled by default (`rerank_enabled` false, overlap weight 0.25, exact-tag boost 0.15, recency boost 0.10).
- Chat-proxy context: internal `knowledge_search` results are assembled through the budgeted context assembler capped at 5 sources, 600 chars per snippet, 8000 total chars, and 2000 estimated tokens (char-based `max(1, len//4)` estimate; per-model tokenizers differ). Tune with `CHAT_CONTEXT_MAX_SOURCES`, `CHAT_CONTEXT_MAX_SNIPPET_CHARS`, `CHAT_CONTEXT_MAX_TOTAL_CHARS`, and `CHAT_CONTEXT_MAX_TOTAL_TOKENS`.
- Chunking: chunk size 2000 (200 to 10000), overlap 200 (0 to 2000, strictly smaller than the size), max chunks 10000.
- Tools: knowledge tools enabled, mutation tools enabled, destructive tools require confirmation.
- Features: semantic retrieval enabled, retrieval explanations enabled.

Activation of a settings draft requires a recent step-up authentication. See [api.md](api.md) for the endpoints.

## Production validation rules

`validate_runtime_safety` runs when `ENVIRONMENT=production` and refuses startup on any of these conditions:

- Wildcard CORS origins.
- Missing or non-HTTPS `PUBLIC_BASE_URL`.
- Missing, wildcard, or empty `TRUSTED_HOSTS`.
- `LOG_LEVEL=DEBUG`.
- Any legacy static API key configured or `LEGACY_API_KEYS_ENABLED=true`.
- No `API_KEY_PEPPERS` entry, a pepper shorter than 32 bytes, or an active version that does not exist.
- No valid 32-byte Fernet key in `MFA_ENCRYPTION_KEYS`, or an active version that does not exist.
- `DB_ECHO=true`.

Never commit real secrets. Use placeholders as shown in `.env.production.example` and generate real values at deployment time.
