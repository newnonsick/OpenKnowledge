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

The client CLI lives in `src/gateway/client.py` and needs only `httpx`.
It is a thin wrapper over the importable `OpenKnowledgeClient` SDK, which
shares one `httpx` session across calls:

```bash
python -m src.gateway.client search "blue valve" --space global --limit 10
python -m src.gateway.client create --space global --title "Valve" --content "Closes clockwise." --tag ops
python -m src.gateway.client update <item-id> --expected-version 1 --title "Valve" --content "Closes clockwise."
python -m src.gateway.client upload --space global --file ./runbook.pdf
python -m src.gateway.client job --job-id <job-id> --watch
python -m src.gateway.client context "blue valve" --space global > context.txt
python -m src.gateway.client fetch <item-id> <revision-id>
python -m src.gateway.client resolve "openknowledge://spaces/global/knowledge/<id>/revisions/<rev>"
python -m src.gateway.client archive <item-id> --expected-version 2
python -m src.gateway.client quota --space global
```

Flags override the environment: `--base-url` beats `OPENKNOWLEDGE_URL`
and `--api-key` beats `OPENKNOWLEDGE_API_KEY`.

## Python SDK

Import `OpenKnowledgeClient` from `src.gateway` for the same workflows
without shelling out. One instance shares one `httpx` session; use it as
a context manager. Failures raise `OpenKnowledgeError` carrying
`status_code` and `response_text`:

```python
from src.gateway import OpenKnowledgeClient

with OpenKnowledgeClient() as sdk:  # reads OPENKNOWLEDGE_URL / OPENKNOWLEDGE_API_KEY
    assert sdk.search("blue valve")["hits"] == []
    item = sdk.create("global", "Valve", "Closes clockwise.", tags=["ops"])
    hits = sdk.search("blue valve")["hits"]
    assert item["id"] in [hit["canonical_id"] for hit in hits]
    sdk.update(item["id"], item["version"], "Valve", "Closes clockwise, firmly.")
    sdk.fetch(item["id"], hits[0]["revision_id"])
    sdk.resolve(hits[0]["citation_uri"])
    sdk.archive(item["id"], item["version"] + 1)
```

Available methods: `search`, `create`, `update`, `upload`, `jobs`,
`watch_job`, `context`, `fetch`, `resolve`, `archive`, `quota`.
`jobs` lists ingestion jobs (optionally filtered by `space`/`job_id`);
`watch_job` polls until the job reaches a terminal state and raises
`OpenKnowledgeError` when the job is not visible to the key.

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
path as REST: auth, scopes, space grants, permission profiles, and
per-credential quotas apply identically. Discovery lives at
`GET /mcp/discovery`, the version matrix at `GET /mcp/versions`, and the
OAuth protected-resource metadata at
`GET /.well-known/oauth-protected-resource/mcp`.

### MCP auth flows

Two bearer flows authenticate MCP tools, both enforced through the same
scope, grant, and quota path as REST:

- Personal API key bridge for private and local harnesses:
  `Authorization: Bearer <personal-api-key>` or
  `X-API-Key: <personal-api-key>`. Key scopes (`knowledge:read`,
  `knowledge:write`) and per-key space grants are intersected with space
  membership on every tool call.
- OIDC bearer for remote and SSO harnesses: when `OIDC_ENABLED=true`,
  `Authorization: Bearer <oidc-access-token>` is verified with the
  configured issuer JWKS (`src/gateway/application/services/oidc_service.py`
  `verify_id_token`), the audience must equal `OIDC_CLIENT_ID`, and the
  token provisions (or syncs) the member with group-to-space grants before
  the tool runs. If the token carries a `scope` (or `scp`) claim, the
  session principal is narrowed to those scopes, so a `knowledge:read`
  token calling a write tool is challenged with `resource_unavailable`;
  tokens without a scope claim keep full session scopes gated by
  membership. Expired, wrong-audience, and untrusted-signature tokens are
  rejected as `invalid_api_key`. The protected-resource metadata
  advertises the issuer in `authorization_servers` only when OIDC is
  enabled; otherwise it stays empty because the gateway uses personal API
  keys rather than OAuth. Auth failures surface as MCP tool errors
  carrying the gateway code instead of `WWW-Authenticate` challenges.

Legacy gateway keys and session cookies are rejected on MCP tools.
Browser `Origin` headers are rejected; MCP is a server-to-server surface.

### MCP compatibility matrix

Pinned spec version: `2026-07-28`. Protocol versions `2024-11-05`
through `2025-11-25` negotiate via `initialize`; `2026-07-28` uses the
modern per-request envelope. The transport is stateless: every request
carries its own auth headers, there is no session tracking or
resumption, no SSE subscription stream, and client notifications are
acknowledged and dropped.

| Client | Transport | Spec | Status |
|---|---|---|---|
| `python-sdk-mcp==2.2.0` (`ClientSession` over `streamable_http_client`; initialize plus list plus call) | streamable-http | `2026-07-28` | tested |
| `raw-http-streamable` (raw httpx JSON-RPC 2.0 POST with `Accept: application/json, text/event-stream`; SSE frame decode) | streamable-http | `2026-07-28` | tested |

The same matrix is served at `GET /mcp/versions` (`clients`,
`pinned_spec_version`) and embedded in `GET /mcp/discovery`. A
conformance test runs both clients against the same corpus with a grant
boundary and a mid-task revoke
(`tests/integration/test_mcp_adapter.py::test_mcp_dual_harness_conformance_with_grant_boundary_and_revoke`).

With the Python MCP SDK (`pip install "mcp>=2.2.0"`):

```python
import os

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

http = httpx.AsyncClient(
    base_url="http://127.0.0.1:8000",
    headers={"Authorization": f"Bearer {os.environ['OPENKNOWLEDGE_API_KEY']}"},
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
