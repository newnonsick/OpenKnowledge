# Installation

This document covers prerequisites and setup for local development and for running the full stack. Production deployment with Compose is covered in [deployment.md](deployment.md); this page focuses on getting a working environment.

## Prerequisites

| Requirement | Version | Evidence |
|---|---|---|
| Python | 3.11 or newer (`requires-python = ">=3.11"`); CI and Docker images use 3.13 | `pyproject.toml`, `Dockerfile`, `.github/workflows/quality.yml` |
| PostgreSQL | The repository pins `pgvector/pgvector:pg17` in Compose and CI; no other minimum is declared | `compose.yaml`, `.github/workflows/quality.yml` |
| Node.js | 24 for the web console and browser tests | `web/Dockerfile`, CI setup-node |
| npm | Included with Node; the repository has `web/package-lock.json` | `web/package.json` |
| Docker Engine with Compose | Any version supporting `compose.yaml` with `mem_limit`, `pids_limit`, and pinned digests | `compose.yaml` |

The migration creates the pgvector extension itself, but the extension binary must be installed in the cluster (the `pgvector/pgvector` image includes it).

## Local development setup

### 1. Clone and create a virtual environment

```bash
git clone <repository-url>
cd my_harness

python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1
```

### 2. Install dependencies

Runtime install with hash verification:

```bash
pip install --require-hashes -r requirements/runtime.lock
```

Development install (includes pytest and related tooling):

```bash
pip install --require-hashes -r requirements/dev.lock
```

The compatibility `requirements.txt` at the repository root simply points to `requirements/dev.lock`. Dependency ranges live in `pyproject.toml`; reviewed, hash-pinned resolutions for Python 3.13 are kept under `requirements/`. The regeneration procedure with pip-tools is documented in `requirements/README.md`.

To verify the lockfile matches the manifest on Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/verify_dependencies.ps1
```

### 3. Configure the environment

```bash
cp .env.example .env
```

Edit `.env` and at minimum set:

- `DATABASE_URL` to your PostgreSQL instance.
- `LLM_URL` and `EMBEDDING_URL` to your inference endpoints.
- `API_KEY_PEPPERS` as a JSON map of version number to pepper string, for example `{"1":"<at least 32 random characters>"}`. Personal API keys are hashed with this pepper; changing it later invalidates existing keys.
- `MFA_ENCRYPTION_KEYS` as a JSON map of version number to a base64url-encoded 32-byte Fernet key, for example `{"1":"<generated Fernet key>"}`. It backs TOTP factor secrets and is required in production.

The full variable reference is in [configuration.md](configuration.md).

### 4. Create the database and apply migrations

Create an empty database in PostgreSQL:

```sql
CREATE DATABASE gateway_db;
```

Apply migrations and verify the schema:

```bash
python -m src.gateway.cli migrate
python -m src.gateway.cli ensure-embedding-generation
python -m src.gateway.cli check
```

`migrate` uses `MIGRATION_DATABASE_URL` when set, otherwise `DATABASE_URL`. The `check` command prints the current revision and exits non-zero if the schema is incompatible. Migrations never run at web startup; running them is always an explicit step.

### 5. Create the first administrator

```bash
python -m src.gateway.cli bootstrap-super-admin --username admin --display-name "Administrator"
```

The command prints a member id, a temporary password, and its expiry. The password must be changed at first login; because the account is a super admin, TOTP enrollment is also required before the session becomes unrestricted. The secret is only printed to an interactive terminal unless `--allow-secret-output` is passed.

If the administrator is locked out, a new temporary password can be issued:

```bash
python -m src.gateway.cli recover-super-admin --username admin
```

### 6. Start the services

Three processes make up a complete development environment:

```bash
# Terminal 1: gateway API
uvicorn src.gateway.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2: background worker (uses WORKER_DATABASE_URL)
WORKER_DATABASE_URL=postgresql+asyncpg://gateway_worker:worker-password@localhost:5432/gateway_db \
  python -m src.gateway.worker

# Terminal 3: web console
cd web
npm ci
npm run dev
```

Notes:

- The worker requires `WORKER_DATABASE_URL` and refuses to start if its username equals the one in `DATABASE_URL`. Create a second PostgreSQL login role (for example `gateway_worker`) in your development database; the exact privileges it needs in production are listed in `deploy/grant-runtime.sql`. For quick local experiments, a superuser role with a distinct name also satisfies the check.
- The worker is optional for manual gateway testing; document ingestion jobs simply remain queued until a worker runs.
- The web console dev server proxies `/api/*` to `http://127.0.0.1:8000` by default; override with `GATEWAY_INTERNAL_URL`.
- Interactive API docs are at `http://localhost:8000/docs`; the console dev server is at `http://localhost:3000`.

### 7. Create an API key for agent traffic

Log in to the console at `http://localhost:3000`, complete the first-use password change and MFA enrollment, and create a personal API key (the first key is also offered automatically after enrollment). Use that key as the bearer token for `/v1/chat/completions` calls.

## Legacy development keys

For quick local experiments, static keys can be configured with `GATEWAY_API_KEYS` and enabled with `LEGACY_API_KEYS_ENABLED=true`. This mechanism exists only for development and test environments; production startup validation rejects it. Prefer personal API keys.

## Running the full stack with Compose

The Compose stack in `compose.yaml` is the production topology: PostgreSQL, one-shot migration and permission services, gateway, worker, web console, and Caddy edge. It requires a fully populated `.env.production`; see [deployment.md](deployment.md) for the procedure and the safety checks involved.

## Web console development

```bash
cd web
npm ci
npm run dev              # development server
npm test -- --run        # vitest component tests
npm run typecheck        # next typegen + tsc --noEmit
npm run build            # production build
npx playwright install chromium
npm run test:browser     # Playwright browser and accessibility suite
```

The typed API client is generated from the exported OpenAPI document:

```bash
# from the repository root
uv run --no-sync --env-file .env python -m scripts.export_openapi --output web/lib/generated/openapi.json

cd web
npm run generate:api
npx tsc --noEmit
```

CI fails if `web/lib/generated/openapi.ts` does not match the exported document, so regenerate and commit both files together after changing an HTTP schema.
