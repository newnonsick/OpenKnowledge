# Deployment

This document describes the production deployment defined by the repository, the CI/CD pipelines, and the surrounding operational tooling. The operator runbook with recovery objectives and procedures is [operations.md](operations.md); this page focuses on what exists and how the pieces fit together.

## Compose topology

`compose.yaml` defines the production stack (project name `openknowledge`) with six long-running or one-shot services:

| Service | Image | Notes |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg17`, digest-pinned | Init script creates the runtime, worker, and maintenance roles; health-checked; internal `data` network only |
| `migrate` | Built from the root `Dockerfile` | One-shot programmatic Alembic migration; runs as uid 10001, read-only rootfs |
| `permissions` | pgvector image | One-shot `psql` applying `deploy/grant-runtime.sql` least-privilege grants after migration |
| `gateway` | Built from the root `Dockerfile` | uvicorn on port 8000; waits for permissions; health check polls `/healthz/ready`; shared storage volume; `app` and `data` networks |
| `worker` | Same image as gateway | Runs `python -m src.gateway.worker`; waits for gateway health; internal network only |
| `web` | Built from `web/Dockerfile` | Next.js standalone server on port 3000; `GATEWAY_INTERNAL_URL` points at the gateway |
| `edge` | `caddy:2.10-alpine`, digest-pinned | Publishes host ports 80, 443 (TCP and UDP for HTTP/3); serves `{$PUBLIC_DOMAIN}` with automatic TLS |

Hardening applied to every application container: non-root user, read-only root filesystem with tmpfs on `/tmp`, all Linux capabilities dropped, `no-new-privileges`, CPU, memory, and PID limits. The `data` network is marked internal, so the database and worker are unreachable from outside the host.

The `edge` routes by path:

- `/v1/*` and `/health*` go to the gateway (agent traffic and health probes).
- Everything else goes to the web console, which proxies its `/api/*` routes to the gateway internally.

## Images

The gateway image is a two-stage build on `python:3.13-slim` (digest-pinned): the builder compiles wheels from `requirements/runtime.lock` with hash verification, and the runtime installs only those wheels, copies `src`, `alembic`, and configuration, and runs as uid 10001. The default command runs uvicorn with `--no-proxy-headers` because TLS termination and host filtering happen at the edge.

The web image is a three-stage build on `node:24-bookworm-slim`: dependency install with `npm ci`, Next.js standalone build, and a runtime containing only the standalone output and static assets, running as the `node` user on port 3000.

## Production deployment procedure

1. Provision a host with Docker and Compose, DNS pointing at it, and outbound access to your LLM and embedding endpoints.
2. Create `.env.production` from `.env.production.example`. Required values include: `PUBLIC_DOMAIN`, `PUBLIC_BASE_URL` (HTTPS), `TRUSTED_HOSTS`, `TRUSTED_PROXY_CIDRS` (the Compose `app` subnet, `172.30.0.0/24` by default), `CORS_ORIGINS`, three independent PostgreSQL passwords, an API key pepper of at least 32 random characters, and a Fernet MFA key.
3. Start the stack:

```bash
docker compose --env-file .env.production up --build -d
```

The startup order is enforced by dependencies: `postgres` becomes healthy, `migrate` completes, `permissions` completes, then `gateway` (which must pass its readiness health check) and `worker` start, then `web`, and finally the edge accepts traffic.

4. Bootstrap the first super admin from the gateway container:

```bash
docker compose --env-file .env.production exec gateway \
  python -m src.gateway.cli bootstrap-super-admin --username admin --display-name "Admin"
```

The command prints a temporary password; first login enforces a password change and TOTP enrollment.

5. Verify `https://<domain>/healthz/ready` returns `ready`.

Runtime configuration for the gateway and worker comes from the env file referenced by `APP_ENV_FILE` (default `.env.production`) plus the service-level database URLs composed from the dedicated role passwords.

## Scaling notes

- The gateway is stateless apart from its database connections and can be scaled horizontally behind the edge; the in-process metrics registry aggregates per instance.
- The worker supports multiple replicas: jobs are claimed through leases with heartbeats, and the outbox dispatcher uses the same discipline.
- Uploaded files live on a shared volume; running gateway or worker replicas outside a single host requires replacing the local storage adapter's backing with shared storage.

## CI/CD workflows

All workflows live in `.github/workflows` and pin actions by commit hash.

| Workflow | Trigger | Purpose |
|---|---|---|
| `quality.yml` | Push and pull request | Python suite (pgvector service), web build, generated-client drift check, and the Windows browser suite. The required release gate. |
| `security.yml` | Push to `main`, pull requests, weekly schedule, manual | pip-audit over `runtime.lock`, production `npm audit` at high severity, Trivy filesystem and image scans (HIGH and CRITICAL gate), and SPDX SBOM generation for both images. |
| `load.yml` | Manual dispatch | k6 load budget against an isolated production-shaped target (`load-test` environment); records latency budgets and run metadata in the summary. |
| `live-provider-quality.yml` | Weekly schedule and manual | Exercises the configured live LLM and embedding endpoints (`live-provider-quality` environment): fixture version, semantic recall, ANN target recall, and ANN-to-exact overlap. |
| `restore-drill.yml` | Monthly schedule and manual | Restores the newest backup into an isolated database and verifies readiness, integrity, retrieval, and recovery time. |
| `production-restore-rehearsal.yml` | Monthly schedule and manual | Full rehearsal on a self-hosted `recovery` runner in the `production-recovery` environment: off-host backup, separate age identity, fresh database, checksum and age verification, restore, migrations, readiness, and metric publication. |
| `legacy-contracts.yml` | Monthly schedule and manual | Evidence run of the quarantined pre-v1 contract tests excluded from the release gate until their 2026-10-31 deadline. |

A release is blocked by test or compilation failure, a high or critical vulnerability finding, container misconfiguration, detected secrets, or failure to produce the SBOM artifacts. A skipped or unconfigured scheduled workflow (for example, a missing recovery runner) counts as a failed control, not a pass.

## Backup and restore

`scripts/backup.sh` produces one age-encrypted bundle per run containing a custom-format database dump, the object storage tree, a database-bound storage manifest, metadata with the Alembic revision, and SHA-256 checksums. Preconditions enforced by the script:

- `BACKUP_WRITERS_QUIESCED=true` (gateway and worker stopped).
- A backup database role with `BYPASSRLS` or superuser (row-filtered roles are refused).
- `BACKUP_DATABASE_URL`, `BACKUP_DIR`, `BACKUP_AGE_RECIPIENT`, and `STORAGE_DIR` set.
- Optional `BACKUP_METRICS_FILE` writes a `gateway_backup_last_success_unixtime` gauge for external monitoring.

`scripts/restore.sh` decrypts and verifies a bundle, then restores into a target that must be a newly created empty database and an existing empty dedicated directory, confirmed by `RESTORE_CONFIRM_ISOLATED=true` and `RESTORE_EXPECTED_DATABASE`. Optional `RESTORE_EXPECTED_BACKUP_SHA256` and `RESTORE_MAX_BACKUP_AGE_SECONDS` tighten the checks. Supporting tooling: `scripts/restore_drill.py` orchestrates a full drill, `scripts/recovery_verify.py` and `scripts/recovery_roles_verify.py` verify the restored state, and `scripts/recovery_metric.py` publishes recovery timestamps.

The operational cadence (hourly backups, monthly verified restores, one-hour RPO and four-hour RTO targets) is specified in [operations.md](operations.md).

## Monitoring and alerting

- The gateway exposes Prometheus metrics at `/metrics`: request counters and latency by route and status, active request gauge, and authentication events. Logs are structured JSON with request ids and W3C trace headers.
- `deploy/prometheus/alerts.yml` ships the alert catalog: readiness failure, SLO error-budget burn, repeated authentication throttling, refresh-token reuse, ingestion queue growth, terminal ingestion failures, dependency failure, stale backup age, certificate expiry, and failed or stale restore rehearsals.
- The intended collection setup, described in [operations.md](operations.md), scrapes backup and restore timestamps through a host-only node-exporter textfile collector and TLS expiry through a blackbox probe.

## Migration procedure in production

For a database migration: take a fresh backup, stop writers, run the one-shot migration and permissions services, verify `/healthz/ready`, then restart the API and worker. Roll forward with a corrective migration when data has changed; use an Alembic downgrade only after proving it against a restored production-shaped copy.
