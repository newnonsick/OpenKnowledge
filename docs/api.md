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

Management list endpoints use server-side numeric pagination with `page` and `page_size` query parameters. Responses contain `items`, the effective `page`, the effective `page_size`, `total_items`, and `total_pages`; an out-of-range page is clamped to the last available page, so the returned `page` may differ from the requested one. Page numbers start at 1 and `page_size` is limited to 100. Management endpoints that mutate state require an `Idempotency-Key` header (1 to 128 characters) so retries do not duplicate work. The read-only `POST /api/v1/retrieval/search` is the exception.

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

Responses follow the OpenAI schema. The orchestrator resolves internal knowledge tool calls before answering, so tool activity is invisible to the client. External tool calls come back with `finish_reason: tool_use` for the client to execute. A `workspace_id` field in the request body scopes the conversation to a specific space.

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

### Models

```bash
curl https://gateway.example.com/v1/models \
  -H "Authorization: Bearer openknowledge_v.example-key"
```

Returns the registered models. The `default` alias resolves to the configured backend model; unregistered names fall back to it rather than failing.

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
| POST | `/api/v1/auth/mfa/confirm` | `factor_id`, `code`, current credentials | Completes enrollment, returns recovery codes and the first personal API key. |
| POST | `/api/v1/auth/logout` | empty | Revokes the session family and clears cookies. |

Login responses report `requires_password_change` and `requires_mfa_enrollment` flags that the console uses to drive its first-use flows.

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
| GET | `/api/v1/operations/summary` | Operational counts and health summary for the dashboard. |

### Spaces and membership

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/spaces` | Spaces visible to the caller. |
| POST | `/api/v1/spaces` | Create a space; the creator becomes its owner. |
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
| GET | `/api/v1/knowledge` | List knowledge items with server-side numeric pagination. |
| POST | `/api/v1/knowledge` | Create an item (first revision). |
| GET | `/api/v1/knowledge/{item_id}` | Item detail with content and metadata. |
| PUT | `/api/v1/knowledge/{item_id}` | Append a revision; requires `expected_version`. |
| DELETE | `/api/v1/knowledge/{item_id}` | Soft delete; requires `expected_version`. |

### Retrieval

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/retrieval/search` | Hybrid search over knowledge and document chunks, honoring the caller's space authorization and the active runtime settings. |

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

Available scopes include `knowledge:read`, `knowledge:write`, `spaces:read`, `spaces:write`, `spaces:members`, `api_keys:write`, `sessions:write`, and `members:write`.

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

### AI management actions

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/ai-tools` | List typed management tools the caller may execute, with their scopes. |
| POST | `/api/v1/ai-tools/{tool_name}` | Execute a management tool; destructive tools return a pending action instead of applying immediately. |
| GET | `/api/v1/ai-actions` | List pending AI actions awaiting confirmation. |
| POST | `/api/v1/ai-actions/{action_id}/confirm` | Confirm and apply a pending action. |

This facade lets an AI assistant perform management tasks through the same authorization, confirmation, and audit controls as the console. Destructive operations require explicit member confirmation.
