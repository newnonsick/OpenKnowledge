# Headless usage

This page shows how to use OpenKnowledge without the web console. Every
workflow below works while the console is stopped: the edge routes
`/api/*`, `/v1/*`, and `/health*` directly to the gateway.

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
