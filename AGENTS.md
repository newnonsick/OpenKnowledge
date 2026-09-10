# Repository Guidelines

## Project Structure & Module Organization

OpenKnowledge provides a FastAPI gateway and Next.js console.

- `src/gateway/`: `domain/` models; `application/` services and ports; `infrastructure/` persistence and adapters; `presentation/` HTTP routers and schemas. Keep dependencies pointing inward.
- `web/app/`, `web/components/`, and `web/lib/`: routes, reusable UI, and client utilities. Follow `web/AGENTS.md` and consult the installed Next.js guides before frontend changes.
- `tests/`: Python unit, integration, end-to-end, operations, and load suites. Frontend tests live in `web/tests/`.
- `alembic/`, `deploy/`, and `scripts/`: migrations, deployment configuration, and operational tools. Documentation and screenshots live in `docs/` and `docs/images/`.

## Build, Test, and Development Commands

Use Python 3.13 and Node.js 24 to match CI. Run Python commands from the repository root and npm commands from `web/`.

| Command | Purpose |
| --- | --- |
| `python -m pip install --require-hashes -r requirements/dev.lock` | Install locked Python dependencies. |
| `python -m src.gateway.cli migrate` | Apply database migrations explicitly. |
| `uvicorn src.gateway.main:app --reload` | Start the local API. |
| `python -m src.gateway.worker` | Run background jobs; configure `WORKER_DATABASE_URL`. |
| `npm ci` / `npm run dev` | Install frontend dependencies / start the console. |
| `npm run typecheck` / `npm run build` | Check TypeScript / build production assets. |

Initialize services using `docs/installation.md`.

## Coding Style & Naming Conventions

Python uses four-space indentation, type hints, `snake_case` functions/modules, and `PascalCase` classes. TypeScript uses two spaces, double quotes, semicolons, `PascalCase` components, and kebab-case filenames. Strict mode is enabled; no dedicated formatter or linter is configured.

## Testing Guidelines

Run `pytest tests/unit` for service-free checks and `pytest` for the full suite. Integration tests need PostgreSQL with pgvector via `TEST_DATABASE_URL` or `DATABASE_URL`. Use `test_*.py`; pytest-asyncio handles async tests. `pytest --cov` measures gateway coverage; no minimum percentage is configured.

Run `npm test` for Vitest/Testing Library tests (`*.test.ts[x]`). Install Chromium with `npx playwright install chromium`, then run `npm run test:browser` for browser/accessibility tests (`*.spec.ts`). Add regression coverage for behavior changes; see `docs/testing.md`.

## Commit & Pull Request Guidelines

Prefer imperative subjects with common prefixes such as `docs:`, `fix:`, or `feat(auth):`; history also includes unprefixed subjects. Keep commits focused. PRs should explain changes and validation, linking relevant issues and including UI screenshots where helpful. Update affected documentation, migrations, and generated OpenAPI artifacts.

## Security & Configuration

Copy `.env.example` to `.env` and configure local credentials. Keep secrets and local data untracked. Follow `requirements/README.md` when regenerating dependency locks.
