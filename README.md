# AI Knowledge Gateway

AI Knowledge Gateway is a self-hosted API service that sits between coding agents and LLM inference backends. On the client side it speaks the OpenAI chat protocol (`/v1/chat/completions`) and the Anthropic messages protocol (`/v1/messages`), so tools such as Cursor, Continue, or Roo Code, and any custom harness, can point at it without code changes. On the backend side it forwards traffic to any OpenAI-compatible inference server: vLLM, Ollama, Text Embeddings Inference, or a hosted endpoint.

What separates it from a plain proxy is the knowledge subsystem. The gateway attaches five internal tools to every conversation and executes them itself against PostgreSQL:

- `knowledge_search` runs hybrid retrieval (lexical full-text search plus vector similarity)
- `knowledge_get` returns the full content and metadata of a stored item
- `knowledge_save` creates a new persistent knowledge item
- `knowledge_update` appends a new revision guarded by optimistic concurrency control
- `knowledge_delete` soft-deletes an item while preserving its history

The model uses these tools to carry context across sessions: project decisions, coding standards, environment details, user preferences, anything worth remembering. The client harness never sees these calls. Tool calls that are not knowledge tools are passed straight back to the client, so existing agent workflows keep working unchanged.

The whole system is one FastAPI service plus one PostgreSQL database with pgvector. There is no external SaaS dependency.

## Features

- Dual protocol support: OpenAI and Anthropic endpoints, both in JSON and Server-Sent Events streaming mode.
- Transparent tool interception. Internal knowledge tools are resolved inside the gateway within the same request. External tool calls are returned to the client with `finish_reason: tool_use` for local execution.
- Hybrid retrieval. PostgreSQL full-text search (`tsvector` with GIN indexes) and pgvector cosine search run in parallel and are fused with weighted Reciprocal Rank Fusion (k=60).
- Knowledge versioning. Every write creates an immutable revision with a SHA-256 content hash. Updates and deletes require `expected_version` and fail with a conflict instead of overwriting newer data.
- Workspaces. Knowledge is scoped per workspace, with a global scope that is visible everywhere. The default `global` workspace is created at startup.
- Document ingestion. Upload `.txt`, `.md`, `.pdf`, `.json`, or source code files; they are parsed, chunked, embedded, and indexed for search automatically.
- Model registry. The `default` alias resolves to the configured backend model, and unregistered model names fall back to it rather than failing.
- Reasoning model support. `reasoning_content` (OpenAI style) and `thinking` blocks (Anthropic style) pass through in both directions, including streams.
- System prompt directive. A knowledge usage directive is appended to the system prompt so the model knows when to search, save, update, and delete. It can be replaced or disabled through configuration.
- API key authentication with constant-time comparison, via `Authorization: Bearer` or `x-api-key`.
- Guardrails. Tool loops are capped by `MAX_TOOL_ITERATIONS`, upstream calls carry their own timeouts, and database access runs through a bounded connection pool.

## How a request is handled

1. The request arrives on `/v1/chat/completions` or `/v1/messages` and is converted into an internal canonical format. Protocol-specific details stop at the presentation layer.
2. The gateway appends the five knowledge tool schemas to whatever tools the client sent and forwards the combined list upstream, together with the composed system prompt.
3. If the model responds with only internal tool calls, the gateway executes them against the database, appends the results to the conversation, and calls the model again. The client sees none of these round trips.
4. If the model responds with an external tool call, the gateway returns it to the client and ends the turn. The client executes the tool and sends back the result in its next request, as usual.
5. The loop stops when the model produces a plain answer or the iteration cap is reached.

In streaming mode, internal tool rounds are buffered and never emitted. The client stream contains only final text, thinking deltas, and external tool calls.

## Architecture

The codebase is organized in four layers with dependencies pointing inward. The domain layer has no framework imports. The infrastructure layer implements interfaces declared in the application layer.

```
            +---------------------------+
            |  Client harness (agent)   |
            +------------+--------------+
                         |
     OpenAI protocol     |     Anthropic protocol
     /v1/chat/completions|     /v1/messages
                         v
            +---------------------------+
            | Presentation (FastAPI)    |
            | auth middleware, routers, |
            | protocol converters       |
            +------------+--------------+
                         |
                         v
            +---------------------------+
            | Application (use cases)   |
            | chat orchestrator,        |
            | knowledge, retrieval+RRF, |
            | ingestion, model registry |
            +-----+---------------+-----+
                  |               |
                  v               v
      +-----------+----+   +------+------------+
      | Infrastructure |   | Infrastructure    |
      | HTTP clients   |   | PostgreSQL        |
      | (LLM, embed)   |   | + pgvector        |
      +----------------+   +-------------------+
```

Layer responsibilities:

- Presentation: FastAPI routers, Pydantic request and response schemas, OpenAI and Anthropic converters, and the API key middleware.
- Application: the chat loop, knowledge and retrieval services, ingestion, the model registry, and file parsers. Declares the port interfaces (repositories, clients, storage).
- Domain: canonical message types, entities, tool definitions, prompt composition, and exceptions. Pure Pydantic models with no I/O.
- Infrastructure: the async SQLAlchemy engine and session factory, the programmatic Alembic runner, ORM models and repository implementations, HTTP clients for the LLM and embedding backends, and the local disk storage adapter.

## Repository layout

```
alembic/                  Database migrations (001 initial schema, 002 global documents)
src/gateway/
  main.py                 App factory and lifespan (migrations, workspace bootstrap)
  config.py               Settings loaded from environment and .env
  domain/                 Canonical types, entities, tools, prompts, exceptions
  application/
    ports/                Abstract interfaces: repositories, clients, storage
    services/             Chat orchestrator, knowledge, retrieval, RRF, ingestion
    parsers/              Text, code, JSON, PDF parsers and the chunker
  infrastructure/
    database.py           Async engine and session factory
    migrations.py         Programmatic Alembic runner
    adapters/             HTTP LLM client, HTTP embedding client
    persistence/          ORM models and repository implementations
    storage/              Local disk storage adapter
  presentation/
    auth.py               API key middleware
    converters/           OpenAI and Anthropic protocol adapters
    schemas/              Request and response models
    routers/              chat, messages, files, models, health
tests/
  unit/                   Unit tests
  integration/            Integration tests
  e2e/
    tier1_features/       Feature coverage
    tier2_boundaries/     Boundary and corner cases
    tier3_combinations/   Cross-feature pairwise combinations
    tier4_scenarios/      Realistic end-to-end workflows
    tier5_adversarial/    Failure injection and stress
    harness/              Test runner and in-process mock backends
```

## Requirements

- Python 3.11 or newer
- PostgreSQL 15 or newer with the pgvector extension available in the cluster (the migration runs `CREATE EXTENSION IF NOT EXISTS vector` itself)
- An OpenAI-compatible chat completion endpoint (vLLM, Ollama, and others)
- An OpenAI-compatible embeddings endpoint (Text Embeddings Inference, Infinity, Ollama)

## Installation

```bash
git clone <repository-url>
cd knowledge-gateway

python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .venv\Scripts\Activate.ps1

pip install --require-hashes -r requirements/runtime.lock
```

For development, install `requirements/dev.lock`. The compatibility `requirements.txt` points to that development lock. Dependency ranges remain in `pyproject.toml`; reviewed Python 3.12 resolutions with hashes are kept under `requirements/`.

## Configuration

Copy the template and adjust it for your environment:

```bash
cp .env.example .env
```

### Gateway

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `development` | Runtime profile: `development`, `test`, or `production` |
| `HOST` | `127.0.0.1` | Bind address |
| `PORT` | `8000` | HTTP port |
| `LOG_LEVEL` | `DEBUG` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `STORAGE_DIR` | `./data/storage` | Directory for uploaded files |
| `DEFAULT_WORKSPACE_ID` | `global` | Workspace used when a request does not specify one |
| `PUBLIC_BASE_URL` | unset | Public HTTPS URL; required in production |
| `TRUSTED_HOSTS` | local hosts | HTTP host allowlist; explicit non-wildcard values are required in production |
| `TRUSTED_PROXY_CIDRS` | unset | Proxy networks trusted by an explicitly configured edge path |
| `GATEWAY_API_KEYS` | unset | Temporary development-only legacy keys |
| `LEGACY_API_KEYS_ENABLED` | `false` | Temporary compatibility switch; forbidden in production |
| `CORS_ORIGINS` | local console origins | Explicit browser origins; wildcard is forbidden in production |
| `MAX_REQUEST_BODY_BYTES` | `16777216` | Maximum request body size |
| `MAX_UPLOAD_BYTES` | `10485760` | Maximum uploaded file size |
| `PARSER_MEMORY_LIMIT_BYTES` | `134217728` | Parser memory budget |
| `PARSER_TIMEOUT_SECONDS` | `60` | Parser timeout |
| `MAX_TOOL_ITERATIONS` | `10` | Tool loop cap per request |
| `TOOL_TIMEOUT_SECONDS` | `15.0` | Timeout for internal tool execution |
| `KNOWLEDGE_SYSTEM_PROMPT_ENABLED` | `true` | Append the knowledge directive to the system prompt |
| `KNOWLEDGE_SYSTEM_PROMPT_CUSTOM` | unset | Replace the built-in directive with custom text |

### Database

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/gateway_db` | Async SQLAlchemy connection string |
| `DB_POOL_SIZE` | `20` | Connection pool size |
| `DB_MAX_OVERFLOW` | `10` | Overflow connections beyond the pool |
| `DB_POOL_TIMEOUT` | `30.0` | Connection acquisition timeout in seconds |
| `DB_POOL_RECYCLE` | `1800` | Recycle connections after this many seconds |
| `DB_ECHO` | `false` | Echo SQL statements to the log |

### LLM backend

| Variable | Default | Description |
|---|---|---|
| `LLM_URL` | `http://localhost:8888` | Base URL of the chat completion endpoint |
| `LLM_MODEL_ID` | `default` | Backend model identifier |
| `LLM_API_KEY` | `EMPTY` | Backend API key |
| `CONTEXT_WINDOW` | `8192` | Context window reported by the model registry |
| `LLM_TIMEOUT_SECONDS` | `120.0` | HTTP timeout for inference |
| `LLM_RETRY_ATTEMPTS` | `3` | Maximum attempts for transient connection, 408, 429, and 5xx failures |
| `LLM_RETRY_BACKOFF_SECONDS` | `0.2` | Initial bounded exponential retry delay |
| `LLM_MAX_CONCURRENCY` | `20` | Process-wide concurrent LLM request limit |
| `LLM_BULKHEAD_TIMEOUT_SECONDS` | `1.0` | Maximum wait for LLM concurrency capacity |
| `LLM_CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive failed operations before opening the circuit |
| `LLM_CIRCUIT_RECOVERY_SECONDS` | `30.0` | Open-circuit interval before one recovery probe |
| `LLM_TEMPERATURE` | `0.7` | Default sampling temperature |
| `LLM_MAX_TOKENS` | unset | Default completion token limit |

### Embedding backend

| Variable | Default | Description |
|---|---|---|
| `EMBEDDING_URL` | `http://localhost:7997` | Base URL of the embeddings endpoint |
| `EMBEDDING_MODEL_ID` | `default` | Embedding model identifier |
| `EMBEDDING_API_KEY` | `EMPTY` | Backend API key |
| `EMBEDDING_DIMENSION` | `1024` | Vector dimension, must match the schema |
| `EMBEDDING_BATCH_SIZE` | `32` | Batch size for embedding requests |
| `EMBEDDING_TIMEOUT_SECONDS` | `30.0` | HTTP timeout for embedding calls |
| `EMBEDDING_RETRY_ATTEMPTS` | `3` | Maximum attempts for transient connection, 408, 429, and 5xx failures |
| `EMBEDDING_RETRY_BACKOFF_SECONDS` | `0.2` | Initial bounded exponential retry delay |
| `EMBEDDING_MAX_CONCURRENCY` | `20` | Process-wide concurrent embedding request limit |
| `EMBEDDING_BULKHEAD_TIMEOUT_SECONDS` | `1.0` | Maximum wait for embedding concurrency capacity |
| `EMBEDDING_CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive failed operations before opening the circuit |
| `EMBEDDING_CIRCUIT_RECOVERY_SECONDS` | `30.0` | Open-circuit interval before one recovery probe |

The current schema is `vector(1024)`. `EMBEDDING_DIMENSION` must remain `1024` until a reviewed embedding-generation migration introduces another dimension; readiness fails closed when runtime configuration and schema differ.

## Database setup

Create an empty database:

```sql
CREATE DATABASE gateway_db;
```

Migrations are a deployment step and never run during web startup. Apply and verify them explicitly:

```bash
python -m src.gateway.cli migrate
python -m src.gateway.cli check
```

Web startup performs a read-only revision check. An incompatible schema keeps liveness available but makes readiness fail with `schema_incompatible`.

## Running

```bash
uvicorn src.gateway.main:app --host 0.0.0.0 --port 8000
```

Add `--reload` during development. Interactive API documentation is served at `/docs`.

## Authentication

All endpoints except `/healthz/live`, `/health`, `/v1/health`, `/docs`, `/openapi.json`, and `/redoc` require a key. Send it as either header:

```
Authorization: Bearer <key>
x-api-key: <key>
```

Keys are compared in constant time. Rejected requests on `/v1/messages` return the Anthropic error shape; every other path returns the OpenAI error shape.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/healthz/live` | Process liveness without dependency access |
| GET | `/healthz/ready` | Public single-flight cached readiness using separate database capacity |
| GET | `/health`, `/v1/health` | Dependency-free compatibility liveness aliases |
| GET | `/v1/models` | Registered models and aliases |
| GET | `/v1/models/{model_id}` | Single model details |
| POST | `/v1/chat/completions` | Chat in OpenAI format, JSON or SSE |
| POST | `/v1/messages` | Chat in Anthropic format, JSON or SSE |
| POST | `/v1/files/upload` | Multipart file upload with ingestion |

### Health

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "healthy",
  "database": "connected"
}
```

The endpoint answers 503 with `"status": "unhealthy"` when the database check fails.

### Chat completion, OpenAI format

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gateway-local-dev" \
  -d '{
    "model": "default",
    "messages": [
      {"role": "user", "content": "What database conventions do we follow?"}
    ]
  }'
```

Responses follow the OpenAI schema. A `workspace_id` field can be added to the request body to scope the conversation to a specific workspace.

Streaming:

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-gateway-local-dev" \
  -d '{
    "model": "default",
    "messages": [
      {"role": "user", "content": "Summarize the deployment workflow"}
    ],
    "stream": true
  }'
```

Events are `chat.completion.chunk` objects terminated by `data: [DONE]`. When the backend produces reasoning output, it arrives as `reasoning_content` inside `delta`.

### Messages, Anthropic format

```bash
curl http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: sk-gateway-local-dev" \
  -d '{
    "model": "default",
    "max_tokens": 1024,
    "system": "You are a software architecture assistant.",
    "messages": [
      {"role": "user", "content": "Explain the authentication model"}
    ]
  }'
```

Set `"stream": true` to receive Anthropic SSE events instead.

### File upload

```bash
curl http://localhost:8000/v1/files/upload \
  -H "Authorization: Bearer sk-gateway-local-dev" \
  -F "file=@./docs/architecture.md" \
  -F "workspace_id=project-alpha" \
  -F "is_global=false" \
  -F "tags=[architecture, reference]"
```

Response, status 201:

```json
{
  "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "filename": "architecture.md",
  "file_size": 8214,
  "total_chunks": 18,
  "workspace_id": "project-alpha",
  "is_global": false,
  "mime_type": "text/markdown",
  "created_at": "2026-08-17T09:41:22.518204"
}
```

Files are routed to a parser by filename and MIME type: PDF (via pypdf), JSON, source code, or plain text and Markdown as the fallback. Content is split into chunks of 500 characters with 50 characters of overlap, embedded in batches, and stored in `document_chunks` where both retrieval channels can find it. `tags` accepts a JSON array or a comma-separated string. Files uploaded to the default workspace are treated as global automatically.

### Models

```bash
curl http://localhost:8000/v1/models \
  -H "Authorization: Bearer sk-gateway-local-dev"
```

```json
{
  "object": "list",
  "data": [
    {
      "id": "default",
      "object": "model",
      "created": 1700000000,
      "owned_by": "gateway",
      "context_window": 8192
    }
  ]
}
```

## Internal knowledge tools

| Tool | Key parameters | Purpose |
|---|---|---|
| `knowledge_search` | `query`, `workspace_id`, `limit`, `tags` | Hybrid search over knowledge items and document chunks |
| `knowledge_get` | `item_id`, `version` | Full content and metadata for one item, optionally a specific revision |
| `knowledge_save` | `title`, `content`, `workspace_id`, `is_global`, `tags` | Create a new knowledge item |
| `knowledge_update` | `item_id`, `expected_version`, `content`, `title`, `tags`, `is_global`, `change_summary` | Append a new revision |
| `knowledge_delete` | `item_id`, `expected_version` | Soft delete, history preserved |

The gateway appends a usage directive to the system prompt that describes when to search, save, update, and delete. Set `KNOWLEDGE_SYSTEM_PROMPT_CUSTOM` to replace the text, or `KNOWLEDGE_SYSTEM_PROMPT_ENABLED=false` to disable injection entirely.

## Retrieval

`knowledge_search` runs two queries in parallel:

1. Lexical: the query is sanitized into AND-connected terms and matched against a generated `to_tsvector('english', content)` column with GIN indexes. Results are ranked with `ts_rank_cd`.
2. Vector: the query is embedded by the embedding backend and matched against the pgvector column using cosine distance over an HNSW index.

Both channels search knowledge items and document chunks at once, restricted to the active workspace plus global items. The two ranked lists are then fused with weighted Reciprocal Rank Fusion:

```
score(d) = sum over channels c of  w_c / (60 + rank_c(d))
```

where `w_c` is the channel weight (1.0 by default) and `rank_c(d)` is the position of document `d` in channel `c`. Fused scores are normalized against the top result, and duplicates collapse to their best entry.

## Workspaces, versions, concurrency

- Every knowledge item belongs to a workspace. Items marked `is_global` are readable from every workspace while remaining owned by their origin workspace.
- The first save creates revision 1. Every update appends an immutable revision recording the content, its SHA-256 hash, the author, and an optional change summary. Nothing is overwritten in place.
- `knowledge_update` requires `expected_version`. If the stored version has moved on, the gateway returns a conflict error instead of clobbering the newer revision. `knowledge_delete` accepts the same check.
- Deletion is soft: queries filter out deleted items, but the revision history stays intact for audit.

## Testing

Most unit and protocol-contract tests need no running services. The SQLite fallback compiles PostgreSQL-specific types for fast checks, but it does not prove PostgreSQL full-text search, pgvector operators, HNSW indexes, transaction behavior, or Alembic release flow. Production capability claims require the real PostgreSQL profile. The LLM and embedding endpoints may remain offline for these database tests.

```bash
pytest                                # everything
pytest tests/unit -m unit             # unit tests
pytest tests/integration -m integration
```

To run the integration layer against the PostgreSQL/pgvector URL already configured in `.env`, load it as `TEST_DATABASE_URL` or use the test environment loader. Each test lifecycle creates and drops a guarded unique schema:

```powershell
$env:TEST_DATABASE_URL = $env:DATABASE_URL
pytest tests/integration -m integration
```

The end-to-end suite is organized in five tiers and driven by its own runner:

```bash
python -m tests.e2e.harness.runner --tier all
python -m tests.e2e.harness.runner --tier 1 --feature F7 -v
python -m tests.e2e.harness.runner --tier 2 --fail-fast
python -m tests.e2e.harness.runner --tier all \
  --json-output reports/e2e.json --markdown-output reports/e2e.md
```

| Tier | Focus |
|---|---|
| 1 | Feature coverage, at least five cases per feature |
| 2 | Boundary and corner cases |
| 3 | Cross-feature pairwise combinations |
| 4 | Realistic workflows: RAG question answering, concurrent edits, streaming UI, coding agent sessions |
| 5 | Adversarial hardening and failure injection |

Tests may be mapped to feature IDs such as `F7` (OpenAI endpoint), `F24` (tool interception), and `F29` (thinking pass-through). The runner rejects unknown feature identifiers. `--dry-run` reports collected nodes without claiming they executed or applying pass thresholds. Count-based E2E tiers are contract coverage, not evidence that production PostgreSQL, networking, storage, or deployment behavior was exercised.

## License

Released under the MIT License. See [LICENSE](LICENSE) for details.
