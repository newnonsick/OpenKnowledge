# Testing

The repository carries five testing layers: Python unit and integration tests under pytest, a five-tier end-to-end suite with its own runner, browser tests for the console (Playwright, including accessibility checks), component tests (vitest), and a k6 load budget script. This page describes each layer and how to run it.

## Python tests

Markers declared in `pyproject.toml`:

| Marker | Meaning |
|---|---|
| `unit` | Unit tests, no services required |
| `integration` | PostgreSQL integration tests |
| `live_provider` | Tests that call configured live model providers |
| `tier1` to `tier5` | End-to-end tier classification |
| `feature(id)` | Feature identifier covered by a test, for example `F7` |

```bash
pytest                                # everything
pytest tests/unit -m unit             # unit tests only
pytest tests/integration              # PostgreSQL integration suite
```

Environment:

- Unit and protocol-contract tests need no running services. Without a PostgreSQL URL, tests fall back to SQLite with PostgreSQL-specific types compiled. This fallback does not exercise full-text search, pgvector operators, HNSW indexes, transaction behavior, or the Alembic release flow.
- Integration tests require PostgreSQL with pgvector, reachable through `TEST_DATABASE_URL`, or `DATABASE_URL` loaded from `.env` by the test environment loader. Each test lifecycle creates and drops a guarded unique schema, so the suite can run against a shared instance. The LLM and embedding endpoints may remain offline for the database tests.

```powershell
# Windows PowerShell: run integration tests against the configured database
$env:TEST_DATABASE_URL = $env:DATABASE_URL
pytest tests/integration
```

Directory inventory:

- `tests/unit` (47 test modules): domain logic, converters, orchestrator budgets, config and runtime safety, login throttle, identity security, parsers, RRF, readiness, resilience, streaming, OpenAPI contract, release assets, and more.
- `tests/integration` (36 test modules plus the `postgres_test_database.py` harness): full PostgreSQL behavior, including RLS isolation, worker role privileges, migration release flow, session rotation, retention, restore drills, retrieval quality gates, and live provider tests (marked `live_provider`).

Coverage is configured over `src/gateway`:

```bash
pytest --cov --cov-report=term
```

## End-to-end suite

The suite under `tests/e2e` is organized in five tiers and driven by its own runner:

```bash
python -m tests.e2e.harness.runner --tier all
python -m tests.e2e.harness.runner --tier 1 --feature F7 -v
python -m tests.e2e.harness.runner --tier 2 --fail-fast
python -m tests.e2e.harness.runner --tier all \
  --json-output reports/e2e.json --markdown-output reports/e2e.md
```

| Tier | Directory | Focus |
|---|---|---|
| 1 | `tier1_features` | Feature coverage, at least five cases per feature |
| 2 | `tier2_boundaries` | Boundary and corner cases |
| 3 | `tier3_combinations` | Cross-feature pairwise combinations |
| 4 | `tier4_scenarios` | Realistic workflows: retrieval-augmented answering, concurrent edits, streaming UI, coding agent sessions |
| 5 | `tier5_adversarial` | Failure injection and stress |

The runner bundles an in-process mock server (`tests/e2e/harness/mock_server.py`) for the upstream endpoints. Tests map to feature identifiers such as `F7` (OpenAI endpoint), `F24` (tool interception), and `F29` (thinking pass-through); the runner rejects unknown identifiers. `--dry-run` reports collected nodes without claiming they executed or applying pass thresholds.

Count-based tiers are contract coverage, not evidence that production PostgreSQL, networking, storage, or deployment behavior was exercised.

## Web console tests

```bash
cd web
npm test -- --run          # vitest component tests (login form, session gate, console shells, api client)
npm run typecheck          # next typegen plus tsc --noEmit
npx playwright install chromium
npm run test:browser       # Playwright chromium suite including accessibility checks (@axe-core)
npm run build              # production build
```

The browser suite runs on Windows in CI and includes accessibility assertions. Vitest runs in jsdom.

## Generated client contract

The FastAPI OpenAPI document is the authoritative browser transport contract. After changing any HTTP schema:

```powershell
uv run --no-sync --env-file .env python -m scripts.export_openapi --output web/lib/generated/openapi.json
Set-Location web
npm run generate:api
npx tsc --noEmit
```

CI verifies that `web/lib/generated/openapi.ts` matches a fresh generation from the committed `openapi.json` and fails the build on drift.

## Load budget

`tests/load/k6.js` defines four constant-load scenarios: management listing, retrieval search, chat streaming, and ingestion upload (with idempotency keys). It runs through `.github/workflows/load.yml` against an isolated production-shaped target with a dedicated least-privilege key, or manually with k6:

```bash
GATEWAY_BASE_URL=https://target.example \
GATEWAY_API_KEY=aigw_v.loadtest \
k6 run tests/load/k6.js
```

The gate requires management p95 below 500 ms and retrieval p95 below one second; streaming and ingestion have separate bounded budgets recorded in the workflow summary. Run it only against isolated targets; the ingestion scenario writes data.

## Operational guard tests

`tests/operations/test_restore_guards.sh` verifies that the restore scripts refuse unsafe targets (non-empty directories, wrong database, missing confirmation) without touching real data.

## CI test jobs

The `Quality` workflow runs on every push and pull request:

- Python job on Ubuntu with a pgvector PostgreSQL 17 service: hash-pinned install, `pip check`, `compileall`, and `pytest`.
- Web job on Node 24: `npm ci`, generated client drift check, vitest, typecheck, production build.
- Browser job on Windows: Playwright chromium suite after the web job passes.

Scheduled and environment-gated suites (security scanning, restore drills, and live provider quality) are described in [deployment.md](deployment.md).
