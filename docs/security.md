# Security model

This document describes authentication, authorization, session handling, secrets management, and the protective controls implemented in the codebase. It distinguishes what the application enforces from what the deployment environment is expected to provide.

## Principals and authentication paths

Every authenticated request resolves to a principal of one of these kinds:

- Session principal: a logged-in member, authenticated by the `__Host-aigw-access` cookie.
- API key principal: a member or automation actor, authenticated by a personal API key (`aigw_v` prefix) in `Authorization: Bearer` or `x-api-key`.
- Legacy static key principal: development-only static keys, active only when explicitly enabled.

The authentication middleware (`src/gateway/presentation/auth.py`) processes all non-public paths. Public paths are exactly:

```
/health, /healthz/live, /healthz/ready, /v1/health,
/docs, /openapi.json, /redoc, /favicon.ico, /metrics,
/api/v1/auth/login, /api/v1/auth/refresh
```

Comparisons of presented and stored key material use constant-time functions.

## Password and MFA

- Passwords are hashed with Argon2 (`argon2-cffi`) through `PasswordService`.
- New passwords require lowercase, uppercase, numeric, and ASCII punctuation characters in addition to length and common-choice checks. System-generated temporary passwords satisfy the same policy.
- Members with the `super_admin` system role must complete TOTP enrollment before their session becomes unrestricted, and every login requires a TOTP code or a single-use recovery code.
- TOTP secrets are encrypted at rest with versioned Fernet keys (`MFA_ENCRYPTION_KEYS`); recovery codes are issued once at enrollment confirmation.
- Members created by a super admin and recovered super admins receive temporary passwords that force a password change on first use.

## Sessions

Session issuance and rotation live in `SessionService` with these policy values:

| Property | Value |
|---|---|
| Access token lifetime | 15 minutes |
| Refresh idle lifetime | 7 days |
| Refresh absolute lifetime | 30 days |
| Concurrent rotation grace | 5 seconds |
| Step-up validity | 10 minutes |

Mechanics:

- Login sets three cookies: `__Host-aigw-access` (HttpOnly, Secure, SameSite=Strict), `__Secure-aigw-refresh` (HttpOnly, Secure, SameSite=Strict, path-scoped to `/api/v1/auth/refresh`), and `aigw-csrf` (readable by the console, mirrored in the `X-CSRF-Token` header).
- Refresh rotation is one-time use with reuse detection. Presenting an already-rotated token fails the session; a short grace window tolerates racing tabs.
- Authenticated session mutations verify the `Origin` header and CSRF header. Production uses the exact origin configured by `PUBLIC_BASE_URL`; non-production accepts only the two local console origins when no public URL is configured.
- Session families group related access and refresh credentials, support revocation of a whole family (sign out a device), and are listed and revocable through the management API.
- Sensitive operations require recent step-up: the caller must have re-authenticated with `POST /api/v1/auth/step-up` within the validity window.

## Login throttling

`LoginThrottleService` applies three independent buckets before credential verification:

| Bucket | Failures | Window | Block |
|---|---|---|---|
| Per account | 5 | 15 minutes | 2 seconds, growing to 15 minutes |
| Per client IP | 20 | 15 minutes | 2 seconds, growing to 15 minutes |
| Global | 200 | 1 minute | 1 second |

Client IPs are derived from trusted proxies only when `TRUSTED_PROXY_CIDRS` is configured; otherwise the socket address is used.

## Personal API keys

- Keys are created with an optional scope set and optional expiry; the secret is returned exactly once and stored as a peppered hash.
- Peppers are versioned (`API_KEY_PEPPERS`, `ACTIVE_API_KEY_PEPPER_VERSION`) so rotation does not invalidate all keys at once.
- The first personal key for a new member is issued during the first-use flow (password change or MFA confirmation) and bound to that session family.
- Creation and revocation are idempotent through the `Idempotency-Key` header and audited.

## Authorization

Two layers compose:

1. System role: `super_admin` or `member` (`src/gateway/domain/identity.py`). Only super admins administer members.
2. Space role: `owner`, `editor`, or `reader` on each space. Actions map from roles as follows:

| Role | Actions |
|---|---|
| reader | read space, read content |
| editor | reader actions, update space, write content |
| owner | editor actions, archive space, manage membership |
| super admin only | member administration |

Each action corresponds to a scope name (for example `knowledge:read`, `spaces:members`, `api_keys:write`) enforced by route dependencies. Row-level security policies in PostgreSQL enforce space isolation for runtime queries, so authorization does not depend solely on application logic.

The AI management facade (`/api/v1/ai-tools`, `/api/v1/ai-actions`) runs under the same scopes; destructive tools produce a pending action that a member must confirm, and confirmations are audited.

## Production configuration guards

`validate_runtime_safety` runs at application creation when `ENVIRONMENT=production` and refuses startup on unsafe settings. See [configuration.md](configuration.md) for the full list. The most important constraints:

- Wildcard CORS is forbidden; origins must be explicit.
- `PUBLIC_BASE_URL` must be an HTTPS URL (it anchors CSRF origin checks).
- Trusted hosts must be explicit; debug logging and SQL echo are forbidden.
- Legacy static API keys cannot be enabled.
- A 32-byte API key pepper and a valid 32-byte Fernet MFA key must be configured.

## Transport and edge controls

The application itself binds HTTP and expects TLS termination at the edge. The Compose edge is Caddy with automatic HTTPS on ports 80 and 443, routing only `/v1/*` and `/health*` to the gateway. In this topology:

- The database, worker, and storage never accept connections from outside the host.
- The gateway and worker containers run as non-root with read-only root filesystems, dropped capabilities, and `no-new-privileges`.
- The web console sets a strict Content-Security-Policy with per-request nonces, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and HSTS in production.

When deploying without the bundled edge, the operator must provide equivalent TLS termination, host filtering, and network isolation.

## Request hygiene

- Request bodies above `MAX_REQUEST_BODY_BYTES` are rejected before parsing.
- Uploads are capped at `MAX_UPLOAD_BYTES`; parsing is bounded by memory, wall-clock, CPU-time, page-count, and output-character budgets.
- The chat tool loop is bounded by iteration, call-count, repeated-signature, and wall-clock limits (see [architecture.md](architecture.md)).
- Upstream LLM and embedding calls are wrapped in bulkheads, bounded retries, and circuit breakers so provider failures cannot exhaust process resources.
- Logs are structured JSON with request ids and trace headers; sensitive values are redacted rather than logged.

## Secrets handling

- No secret values are committed; `.env.production.example` uses placeholders only.
- Database credentials are split per role (runtime, worker, admin) and injected by Compose.
- Backup bundles are encrypted with age; the age identity is expected to be kept off-host.
- Rotation procedures for peppers, MFA keys, and database credentials are in [operations.md](operations.md).

## Audit trail

Security-relevant events are written to `audit_events` with the acting principal and request id. Recorded actions include: knowledge changes (`knowledge.create`, `knowledge.update`, `knowledge.delete`), space lifecycle (`space.created`, `space.archived`, `space.membership_changed`, `source.archived`), API keys (`api_key.created`, `api_key.revoked`), member administration (`member.created`, `member.updated`, `member.password_reset`, `member.bootstrap_super_admin`, `member.recover_super_admin`), credentials and MFA (`credential.password_changed`, `mfa.enrollment_started`, `mfa.enrollment_confirmed`, `mfa.recovery_code_consumed`), sessions (`session.revoked`, `session.step_up_completed`, `session.refresh_reuse_detected`), runtime settings (`runtime_settings.draft_created`, `runtime_settings.activated`, `runtime_settings.rolled_back`), and the AI management facade (`ai_action.proposed`, `ai_action.executed`). Retention batches write their own system audit events. The console and `/api/v1/audit-events` expose the trail to authorized members.
