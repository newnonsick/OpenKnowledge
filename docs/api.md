# HTTP API reference

The gateway exposes three API families on one origin:

1. Agent protocol endpoints under `/v1`, authenticated with personal API keys. These follow the OpenAI and Anthropic wire formats.
2. Management endpoints under `/api/v1`, authenticated with a browser session cookie or a personal API key bearer token.
3. Health and metrics endpoints, public.

The exported OpenAPI document at `/openapi.json` is the authoritative contract, and interactive documentation is served at `/docs` and `/redoc`. The web console's TypeScript client is generated from the same document, and CI enforces that the generated client matches the export. Prefer the generated document for exact field-level schemas; this page documents behavior and gives representative examples.

## Conventions

Authentication:

- Protocol and management endpoints accept `Authorization: Bearer <api-key>` (personal API keys, prefix `openknowledge_v`).
- The `x-api-key` header is honored only for the optional legacy static keys, which are disabled by default and rejected in production.
- Management endpoints called from the browser use the `__Host-openknowledge-access` session cookie set at login.
- The login and refresh endpoints are public; everything else except the health endpoints, `/docs`, `/openapi.json`, `/redoc`, `/favicon.ico`, and `/metrics` requires authentication.

Errors use a consistent JSON shape:

```json
{
  "error": {
    "type": "invalid_request_error",
    "code": "resource_conflict",
    "message": "The item was modified by another writer."
  },
  "request_id": "b7c1..."
}
```

Rejected requests on `/v1/messages` return the Anthropic error shape; other paths return the OpenAI error shape. Every response carries a `request_id` that also appears in logs for correlation.

Management list endpoints use server-side numeric pagination with `page` and `page_size` query parameters. Responses contain `items`, the effective `page`, the effective `page_size`, `total_items`, and `total_pages`; an out-of-range page is clamped to the last available page, so the returned `page` may differ from the requested one. Page numbers start at 1 and `page_size` is limited to 100. Management endpoints under `/api/v1` that mutate state (POST, PUT, PATCH, DELETE outside `/api/v1/auth/*`, which uses cookies and CSRF instead) require an `Idempotency-Key` header so retries do not duplicate work: 1 to 128 characters on most routes, up to 255 on `POST /api/v1/sources/upload`. Read-only GET routes and the read-only `POST /api/v1/retrieval/search` take no such header.

## Health and metrics

| Method | Path | Description |
|---|---|---|
| GET | `/healthz/live` | Process liveness, no dependency access. |
| GET | `/healthz/ready` | Readiness: cached single-flight database probe plus schema compatibility. Returns 503 with `code: schema_incompatible` when the schema does not match. |
| GET | `/health`, `/v1/health` | Dependency-free compatibility aliases. |
| GET | `/metrics` | Prometheus text format: request counters, active requests, latency, and authentication events. |

## Agent protocol

### Chat completion, OpenAI format

```bash
curl https://gateway.example.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer openknowledge_v.example-key" \
  -d '{
    "model": "default",
    "messages": [
      {"role": "user", "content": "What database conventions do we follow?"}
    ]
  }'
```

Responses follow the OpenAI schema. The orchestrator resolves internal knowledge tool calls before answering, so tool activity is invisible to the client. External tool calls come back with `finish_reason: tool_calls` for the client to execute. A `workspace_id` field in the request body sets a hard request space scope for the conversation: `global` (the default) searches every space the caller can access, while any other workspace restricts retrieval to that space. Per-call space filters such as the `knowledge_search` tool `workspace_id` argument may narrow this scope but never widen it; a filter outside the scope yields no results. The scope is intersected with the caller's space membership, so inaccessible spaces stay invisible even inside the scope.

Streaming: add `"stream": true`. Events are `chat.completion.chunk` objects terminated by `data: [DONE]`. Reasoning output arrives as `reasoning_content` inside `delta` when the backend produces it.

### Messages, Anthropic format

```bash
curl https://gateway.example.com/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer openknowledge_v.example-key" \
  -d '{
    "model": "default",
    "max_tokens": 1024,
    "system": "You are a software architecture assistant.",
    "messages": [
      {"role": "user", "content": "Explain the authentication model"}
    ]
  }'
```

Set `"stream": true` to receive Anthropic SSE events. Thinking blocks pass through in both directions.

### Responses, OpenAI format

```bash
curl https://gateway.example.com/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer openknowledge_v.example-key" \
  -d '{
    "model": "default",
    "input": "What database conventions do we follow?"
  }'
```

`input` accepts a string or a list of `{role, content}` message dicts with text content only; `content` may be a string or a list of `input_text` parts. Supported fields are `model`, `input`, `stream`, `max_output_tokens`, `metadata` (string map, passed through untouched), and `workspace_id` (same hard request space scope as chat completions). Unknown top-level fields are rejected with `422` and never silently dropped; non-text input parts are rejected with `400 unsupported_input`. `stream: true` is rejected with `400 unsupported_stream` — only non-streaming responses are served. The response is `{id: "resp_...", object: "response", created, model, status: "completed", output: [{type: "message", role: "assistant", content: [{type: "output_text", text}]}], usage}` with `usage` in Responses shape (`input_tokens`, `output_tokens`, `total_tokens`). Token usage is recorded against the same quota as chat completions.

### Models

```bash
curl https://gateway.example.com/v1/models \
  -H "Authorization: Bearer openknowledge_v.example-key"
```

Returns the registered models. The `default` alias resolves to the configured backend model; an empty name or a registered alias resolves the same way, while an unregistered name is rejected with a not-found error.

### Retired endpoint

`POST /v1/files/upload` is retired and answers `410 Gone` with a pointer to `/api/v1/sources/upload`. Use the management sources API for all ingestion.

## Management authentication

| Method | Path | Body | Description |
|---|---|---|---|
| POST | `/api/v1/auth/login` | `username`, `password`, optional `totp_code` or `recovery_code` | Authenticates and sets session cookies. Super admins must supply a second factor. Throttled per account, per IP, and globally. |
| POST | `/api/v1/auth/refresh` | empty | Rotates the refresh token (CSRF header `X-CSRF-Token` required, Origin verified) and issues a new access cookie. |
| POST | `/api/v1/auth/step-up` | `password` plus optional second factor | Re-authenticates and extends step-up authorization for 10 minutes. Required before sensitive operations such as activating settings drafts. |
| POST | `/api/v1/auth/password` | current credentials plus new `password` and `confirmation` | Changes the authenticated member's password and issues replacement session cookies. An unrestricted member supplies `current_password`; an unrestricted super admin also supplies `current_totp_code` or `recovery_code`. A restricted first-use session supplies only the new `password` and `confirmation`. Prior website sessions are revoked while personal API keys are preserved. |
| POST | `/api/v1/auth/mfa/totp/enroll` | current credentials | Starts TOTP enrollment, returns the shared secret and an `otpauth://` provisioning URI that the console renders as a QR code. |
| POST | `/api/v1/auth/mfa/totp/confirm` | `factor_id`, `code`, current credentials | Completes enrollment, returns recovery codes and the first personal API key. |
| POST | `/api/v1/auth/logout` | empty | Revokes the session family and clears cookies. |
| GET | `/api/v1/auth/oidc/login` | empty, requires `OIDC_ENABLED=true` | Starts the OIDC authorization-code flow with PKCE (`S256`) and a signed state cookie, then redirects (302) to the provider. Answers 401 while OIDC is disabled. |
| GET | `/api/v1/auth/oidc/callback` | `code`, `state` query params | Validates state, exchanges the code, verifies the RS256 ID token via JWKS (`iss`/`aud`/`exp`), JIT-provisions or updates the member, syncs the system role and space memberships from IdP groups, and issues the same session cookies as password login. Group removal revokes mapped memberships and disables grant-less members (audited as `member.deprovisioned`); members are never deleted. |

Login responses report `requires_password_change` and `requires_mfa_enrollment` flags that the console uses to drive its first-use flows. OIDC logins always report both flags as `false` and are audited as `oidc.login`.

OIDC membership sync is authoritative: the IdP groups on each login rewrite the mapped memberships and system role, so manual grants on mapped axes are overwritten on the next OIDC login. Authorization codes are single-use at the IdP, which is the replay backstop for the login flow.

### OIDC identity-provider setup

Any standards-compliant provider works (Keycloak, Auth0, Microsoft Entra ID, Okta, Zitadel). Register a confidential web client with the redirect URL `https://<gateway>/api/v1/auth/oidc/callback`, enable the `openid email profile` scopes, and expose group memberships in the ID token under the claim named by `OIDC_GROUP_CLAIM` (default `groups`). Keycloak mapsRealm or client roles through a "Group Membership" mapper; Auth0 adds them with an Action that sets `event.idToken`; Entra ID emits them as the `groups` claim once group assignment is enabled, using role IDs if overage applies. Point `OIDC_ISSUER` at the provider's issuer URL, copy the client id and secret, then map groups with `OIDC_ROLE_MAPPING` (group to `member` or `super_admin`) and `OIDC_GROUP_SPACE_MAP` (group to `[{space_id, role}]`). Verify locally with `PYTHONPATH=. .venv/Scripts/python.exe scripts/storage_drill.py` for storage and the OIDC unit tests before pointing production traffic at the provider.

Cookies set by these endpoints (production names; local development uses unprefixed `openknowledge-access` and `openknowledge-refresh`):

| Cookie | Purpose | Lifetime |
|---|---|---|
| `__Host-openknowledge-access` | Access token for API calls | 15 minutes |
| `__Secure-openknowledge-refresh` | Refresh token | 7-day idle, 30-day absolute, path-scoped to `/api/v1/auth/refresh` |
| `openknowledge-csrf` | CSRF token mirrored in the `X-CSRF-Token` header | Matches the refresh lifetime |

## Management endpoints

All management list endpoints use numeric server-side pagination. Ownership transfers, settings activation, and other destructive or privileged mutations require step-up authentication and are audited.

### Current member and operations

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/me` | The authenticated member, roles, and restrictions. |
| GET | `/api/v1/operations/summary` | Operational counts and health summary for the dashboard, including stale knowledge counts. |
| GET | `/api/v1/operations/reindex` | Embedding generation list with the active id plus re-embed progress per target. |
| POST | `/api/v1/operations/reindex/enqueue` | Enqueue re-embedding for a subset of targets; idempotency-keyed, operator role required. |
| POST | `/api/v1/operations/generations/{id}/promote` | Promote a building generation to active and retire the current one; refused with 409 while re-embed work is pending unless forced with a recorded reason. |
| POST | `/api/v1/operations/generations/rollback` | Reactivate the most recently retired generation and retire the current one. |

### Spaces and membership

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/spaces` | Spaces visible to the caller. |
| POST | `/api/v1/spaces` | Create a space; the creator becomes its owner. |
| GET | `/api/v1/spaces/{space_id}` | Space detail including the effective chunk policy (`source` is `space` when overridden, else `global`). |
| PUT | `/api/v1/spaces/{space_id}/chunk-policy` | Set the space chunk policy (`chunk_size`, `chunk_overlap`, `chunk_strategy: fixed\|semantic`); `overlap < size` and size within 64–32000, otherwise 422. |
| GET | `/api/v1/admin/spaces` | All spaces, including archived (super admin). |
| GET | `/api/v1/spaces/{space_id}/members` | Members of a space. |
| GET | `/api/v1/spaces/{space_id}/member-candidates` | Members who can be added. |
| PUT | `/api/v1/spaces/{space_id}/members/{member_id}` | Add or change a member's space role. |
| PUT | `/api/v1/spaces/{space_id}/ownership` | Transfer ownership (owner only, step-up required). |
| PUT | `/api/v1/admin/spaces/{space_id}/ownership` | Transfer ownership (super admin, step-up required). |
| DELETE | `/api/v1/spaces/{space_id}/members/{member_id}` | Remove a member from a space. |
| DELETE | `/api/v1/spaces/{space_id}` | Archive a space. |

Space roles are `owner`, `editor`, and `reader`. Owners manage membership and can archive; editors read and write content; readers read only.

### Knowledge

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/knowledge` | List knowledge items with server-side numeric pagination; optional `lifecycle_status` filter. |
| POST | `/api/v1/knowledge` | Create an item (first revision); accepts `lifecycle_status` (`observation`, `candidate`, `accepted`), `origin`, `source_detail`, `expires_at`. Pass `enrich_async: true` to commit the revision immediately and enrich (embed + activate retrieval) via a worker job: returns 202 with the item plus `job_id`/`job_state`. Lexical reads hold from commit time; vector projection follows the job. |
| GET | `/api/v1/knowledge/{item_id}` | Item detail with content and metadata, plus `enrichment` status (`pending`, `enriched`, `failed`, or null when never deferred). |
| PUT | `/api/v1/knowledge/{item_id}` | Append a revision; requires `expected_version`. Accepts `review_note` and `expires_at` edits (`update_expires_at`). `enrich_async: true` behaves like create: 202 receipt, lexical-first, job-backed projection. Failed enrichment is retried through the existing `POST /api/v1/ingestion-jobs/{job_id}/retry` endpoint; the job appears in `GET /api/v1/ingestion-jobs` with kind `knowledge_enrichment`. |
| POST | `/api/v1/knowledge/{item_id}/transitions` | Move an item along the lifecycle graph; requires `expected_version`, idempotent via `Idempotency-Key`. Allowed: `observation` to `candidate`/`accepted`, `candidate` to `accepted`/`observation`, `accepted` to `superseded` (requires a review note), `superseded` back to `accepted`. Anything else is refused with 409. Each transition is audited as `knowledge.lifecycle.transitioned`. Replaying a transition after losing space access is denied. |
| DELETE | `/api/v1/knowledge/{item_id}` | Soft delete; requires `expected_version`. |
| GET | `/api/v1/knowledge/stale` | Detection-only stale listing ordered oldest first (`older_than_days` overrides `STALE_AFTER_DAYS`); never purges or mutates knowledge rows. |
| POST | `/api/v1/knowledge/{item_id}/reviewed` | Record a `knowledge.reviewed` audit event only; no knowledge state changes, idempotent via `Idempotency-Key`. |

Knowledge `expires_at` values without timezone info are interpreted as UTC. Portable export/import preserves lifecycle fields and rejects overlong titles, tags, or lifecycle metadata with 422.

### Retrieval

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/retrieval/search` | Hybrid search over knowledge and document chunks, honoring the caller's space authorization and the active runtime settings. `active_space_id` sets a hard request space scope (omitted means every accessible space); `space_ids` may narrow that scope but never widen it. `observation` and `candidate` items are searchable; `superseded` items are excluded from search while their citations stay resolvable through exact evidence fetch (`superseded: true`). Lifecycle and staleness are independent axes. |

```bash
curl https://gateway.example.com/api/v1/retrieval/search \
  -H "Authorization: Bearer openknowledge_v.example-key" \
  -H "Content-Type: application/json" \
  -d '{"query": "backup procedure", "semantic_policy": "prefer", "limit": 10}'
```

### Sources and ingestion

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/sources/upload` | Multipart upload; requires `Idempotency-Key`. Returns 202 with a document, revision, and job receipt. |
| GET | `/api/v1/sources` | List uploaded sources, optionally filtered by `space_id`. |
| DELETE | `/api/v1/sources/{document_id}` | Delete a source. |
| GET | `/api/v1/ingestion-jobs` | List ingestion jobs with state, attempts, and progress. |
| POST | `/api/v1/ingestion-jobs/{job_id}/cancel` | Request cooperative cancellation. |
| POST | `/api/v1/ingestion-jobs/{job_id}/retry` | Request a retry of a failed job. |
| POST | `/api/v1/sources/connectors` | Register a local git repository to a space (branch-pinned); idempotent via `Idempotency-Key`. |
| GET | `/api/v1/sources/connectors` | List registered connectors, optionally filtered by `space_id`. |
| POST | `/api/v1/sources/connectors/{id}/sync` | Incremental sync at 202: enqueues added/modified files, archives documents for deleted files, skips unchanged/binary/oversize files with reasons. |
| DELETE | `/api/v1/sources/connectors/{id}` | Unregister; already-ingested documents stay preserved. |

Document chunking uses the effective chunk policy: the space override when `PUT /api/v1/spaces/{space_id}/chunk-policy` set one, otherwise the global `INGESTION_CHUNK_SIZE` / `INGESTION_CHUNK_OVERLAP` config (semantic strategy). Invalid policies are rejected with 422 and never partially applied.

Git connector identity is the normalized repository root plus branch per space; sync diffs against the stored commit so repeat syncs enqueue nothing. Ingested documents inherit the connector's space membership with the syncing member as author — per-file ACLs are out of scope for v1, so branch pinning and space membership are the access boundary. No docs-site crawler, hosted-git API, or sync scheduler ships in v1; sync is on-demand and the worker picks jobs up through the existing lease pipeline.

```bash
curl https://gateway.example.com/api/v1/sources/upload \
  -H "Authorization: Bearer openknowledge_v.example-key" \
  -H "Idempotency-Key: 8f14e45f-ea0b-4ad3-9c56-2d1b9c81f003" \
  -F "file=@./architecture-notes.md" \
  -F "space_id=project-alpha" \
  -F "display_name=Architecture notes"
```

Response, status 202:

```json
{
  "document_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "revision_id": "6f9619ff-8b86-d011-b42d-00cf4fc964ff",
  "job_id": "c9bf9e57-1685-4c89-bafb-ff5af830be8a",
  "job_state": "queued",
  "duplicate_candidate_revision_id": null
}
```

Parsing and embedding happen asynchronously in the worker. Track progress through `/api/v1/ingestion-jobs` or the console's ingestion page.

### Personal API keys

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/api-keys` | List the caller's keys (public ids, scopes, expiry; never secrets). |
| POST | `/api/v1/api-keys` | Create a key; requires `Idempotency-Key`. The secret is returned once. |
| DELETE | `/api/v1/api-keys/{key_id}` | Revoke a key; requires `Idempotency-Key`. |

Personal API keys can carry `chat:write`, `knowledge:read`, `knowledge:write`, `spaces:read`, `spaces:write`, `spaces:members`, `api_keys:write`, `settings:read`, and `settings:write`. The `sessions:write` and `members:write` gates on member, session, and audit routes accept browser sessions only; they cannot be granted to an API key.

Create requests accept optional `space_grants` (a list of space ids) and an optional `permission_profile` (`reader`, `project_contributor`, `trusted_maintainer`, `import_worker`, `human_admin`). Keys created with `space_grants` are restricted to the intersection of the owner's member spaces and the granted spaces; keys created without grants keep all member spaces. Key listing exposes `space_grants` (`null` means unrestricted). Profile scopes outside the profile matrix are rejected at creation, and sensitive control operations always require a session principal.

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/quotas/usage?space_id=...` | Current credential's budget usage and limits for the space. |

Exceeded rate, concurrency, storage, or token budgets return `429 quota_exceeded` with a `Retry-After` header. See [security.md](security.md) for the budget table and [configuration.md](configuration.md) for the `QUOTA_*` knobs.

### Members, sessions, audit

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/members` | List members (super admin). |
| POST | `/api/v1/members` | Create a member (super admin); returns a temporary password. |
| PATCH | `/api/v1/members/{member_id}` | Change display name or status (super admin). |
| POST | `/api/v1/members/{member_id}/password-reset` | Issues a 24-hour one-time password for another pending or active account, revokes that account's sessions and API keys, and forces password replacement. Resetting the caller or a disabled account returns `409 resource_conflict`; existing super-admin MFA remains required. |
| GET | `/api/v1/sessions` | List active session families. |
| DELETE | `/api/v1/sessions/{family_id}` | Revoke a session family (sign out a device). |
| GET | `/api/v1/audit-events` | Query the audit trail. |

### Runtime settings

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/settings` | The active settings revision. |
| GET | `/api/v1/settings/history` | Revision history. |
| POST | `/api/v1/settings/drafts` | Create a draft revision (validated, not live). |
| POST | `/api/v1/settings/drafts/{draft_id}/activate` | Activate a draft (step-up required). |
| POST | `/api/v1/settings/rollback/{target_revision}` | Roll back to a previous revision (step-up required). |

The active revision is loaded fresh for every request, so activation and rollback take effect on the next request while in-flight requests finish under the snapshot they started with. There is no cross-request policy cache to invalidate. Settings writes require the `settings:write` scope plus a recent step-up; other scopes cannot change the policy.

Runtime policy flags and the surfaces they govern:

| Flag | Off behavior |
|---|---|
| `knowledge_tools_enabled` | Knowledge search is unavailable: `POST /api/v1/retrieval/search`, the `knowledge.search.v1`, `retrieval.explain.v1`, and `knowledge.read.v1` AI tools (hidden from discovery), and the chat `knowledge_search` tool (not advertised; direct calls fail). Plain knowledge CRUD reads are unchanged. |
| `mutation_tools_enabled` | Knowledge writes are unavailable: `POST/PUT/DELETE /api/v1/knowledge`, source upload/archive, ingestion job cancel/retry, and every state-changing AI tool including confirmation-gated proposals. |
| `destructive_tools_require_confirmation` | When false, confirmation-gated AI tools (`spaces.archive.v1`, `spaces.members.set.v1`, `knowledge.archive.v1`, `ingestion_jobs.cancel.v1`, `ingestion_jobs.retry.v1`, `settings.propose.v1`) execute immediately for website sessions instead of returning a pending action; discovery reports them as `confirmation: none`. Non-session callers still receive a pending action, which only a session of the same member can confirm. The confirmation requirement is bound at proposal time. |
| `semantic_retrieval_enabled` | Vector retrieval is forced off on every surface: requests behave as `semantic_policy: disabled` even when another policy is asked for. |
| `retrieval_explanations_enabled` | Detailed retrieval explanations are redacted: `retrieval.explain.v1` is hidden and rejected, health/explanation payloads keep only the status and abstention flag, and chat search results omit health details. |

### AI management actions

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/ai-tools` | List typed management tools the caller may execute, with their scopes. |
| POST | `/api/v1/ai-tools/{tool_name}` | Execute a management tool; destructive tools return a pending action instead of applying immediately. |
| GET | `/api/v1/ai-actions` | List pending AI actions awaiting confirmation. |
| POST | `/api/v1/ai-actions/{action_id}/confirm` | Confirm and apply a pending action. |

This facade lets an AI assistant perform management tasks through the same authorization, confirmation, and audit controls as the console. Destructive operations require explicit member confirmation.
