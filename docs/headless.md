# Headless usage

This page shows how to use OpenKnowledge without the web console. Every
workflow below works while the console is stopped: the edge routes
`/api/*`, `/v1/*`, `/mcp*`, `/.well-known/oauth-protected-resource/*`,
and `/health*` directly to the gateway.

## Prerequisites

- Gateway reachable at a base URL, e.g. `http://127.0.0.1:8000`.
- A personal API key with the scopes the workflow needs
  (`knowledge:read` for search, `knowledge:write` for writes and uploads).
  Create one in the console once under Settings, then store it as
  `OPENKNOWLEDGE_API_KEY`. Key grants are intersected with space
  membership on every request.

Set the common environment once:

```bash
export OPENKNOWLEDGE_URL=http://127.0.0.1:8000
export OPENKNOWLEDGE_API_KEY=openknowledge_v1_…
```

## CLI

The client CLI lives in `src/gateway/client.py` and needs only `httpx`:

```bash
python -m src.gateway.client search "blue valve" --space global --limit 10
python -m src.gateway.client create --space global --title "Valve" --content "Closes clockwise." --tag ops
python -m src.gateway.client update <item-id> --expected-version 1 --title "Valve" --content "Closes clockwise."
python -m src.gateway.client upload --space global --file ./runbook.pdf
python -m src.gateway.client job --job-id <job-id> --watch
python -m src.gateway.client context "blue valve" --space global > context.txt
```

Flags override the environment: `--base-url` beats `OPENKNOWLEDGE_URL`
and `--api-key` beats `OPENKNOWLEDGE_API_KEY`.

## REST recipes

First search from any harness:

```bash
curl -s "$OPENKNOWLEDGE_URL/api/v1/retrieval/search" \
  -H "Authorization: Bearer $OPENKNOWLEDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"query":"blue valve","limit":10}' | jq .hits
```

Create with optimistic concurrency and idempotency:

```bash
curl -s "$OPENKNOWLEDGE_URL/api/v1/knowledge" \
  -H "Authorization: Bearer $OPENKNOWLEDGE_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: note-2026-09-12-001" \
  -d '{"space_id":"global","title":"Valve","content":"Closes clockwise.","tags":["ops"]}'
```

Upload and follow the durable job:

```bash
curl -s "$OPENKNOWLEDGE_URL/api/v1/sources/upload" \
  -H "Authorization: Bearer $OPENKNOWLEDGE_API_KEY" \
  -H "Idempotency-Key: upload-2026-09-12-001" \
  -F file=@./runbook.pdf -F space_id=global | tee upload.json
JOB=$(jq -r .job_id upload.json)
python -m src.gateway.client job --job-id "$JOB" --watch
```

## MCP recipes

The gateway exposes Streamable HTTP MCP at `/mcp` with the same policy
path as REST: personal API key auth, scopes, space grants, permission
profiles, and per-credential quotas apply identically. Discovery lives at
`GET /mcp/discovery`, the version matrix at `GET /mcp/versions`, and the
OAuth protected-resource metadata at
`GET /.well-known/oauth-protected-resource/mcp`. The metadata advertises
an empty `authorization_servers` list because the gateway uses personal
API keys rather than OAuth: there is no authorization server to direct
clients to, and auth failures surface as MCP tool errors carrying the
gateway code instead of `WWW-Authenticate` challenges.

Available tools: `knowledge.search`, `knowledge.fetch`,
`knowledge.create`, `knowledge.update`, `knowledge.archive`,
`context.assemble`, `evidence.resolve`, `ingestion.status`,
`ingestion.control`. Protocol versions `2024-11-05` through `2025-11-25`
negotiate via `initialize`; `2026-07-28` uses the modern per-request
envelope. The transport is stateless: every request carries its own
auth headers, there is no session tracking or resumption, no SSE
subscription stream, and client notifications are acknowledged and
dropped. Only personal API keys authenticate MCP tools (`Authorization:
Bearer <key>` or `X-API-Key: <key>`); legacy gateway keys and session
cookies are rejected. Browser `Origin` headers are rejected; MCP is a
server-to-server surface.

With the Python MCP SDK (`pip install "mcp>=2.2.0"`):

```python
import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

http = httpx.AsyncClient(
    base_url="http://127.0.0.1:8000",
    headers={"Authorization": "Bearer $OPENKNOWLEDGE_API_KEY"},
)
async with http:
    async with streamable_http_client(
        "http://127.0.0.1:8000/mcp/", http_client=http
    ) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool(
                "knowledge.search", {"query": "blue valve", "limit": 10}
            )
```

Writes need an explicit `idempotency_key` argument; auth, quota, and
grant denials arrive as tool errors carrying the gateway error code
(`resource_unavailable`, `quota_exceeded`, `invalid_api_key`).

## Harness notes

- Reads and writes share one policy path with the console: scope,
  membership, key grants, and runtime flags apply identically.
- `Idempotency-Key` is required for knowledge writes, uploads, and job
  mutations. Search accepts an optional key so a read-only request can
  safely retry after a session refresh.
- Updates and deletes need `expected_version` / `expected_revision`
  from the latest read; concurrent edits conflict instead of silently
  overwriting.
- Capability limits come from `GET /api/v1/capabilities`
  (`max_upload_bytes`, `max_request_body_bytes`).
