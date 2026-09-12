from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Optional, Sequence
from uuid import uuid4

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"


def _base_url(args: argparse.Namespace) -> str:
    return (args.base_url or os.environ.get("OPENKNOWLEDGE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _api_key(args: argparse.Namespace) -> str:
    key = args.api_key or os.environ.get("OPENKNOWLEDGE_API_KEY")
    if not key:
        raise SystemExit("Provide --api-key or set OPENKNOWLEDGE_API_KEY.")
    return key


def _client(base_url: str, api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def execute_search(args: argparse.Namespace) -> int:
    with _client(_base_url(args), _api_key(args)) as client:
        response = client.post(
            "/api/v1/retrieval/search",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "query": args.query,
                "limit": args.limit,
                **({"space_ids": args.space.split(",")} if args.space else {}),
            },
        )
    if response.status_code != 200:
        print(f"Search failed ({response.status_code}): {response.text}")
        return 1
    _print(response.json())
    return 0


def execute_create(args: argparse.Namespace) -> int:
    with _client(_base_url(args), _api_key(args)) as client:
        response = client.post(
            "/api/v1/knowledge",
            headers={"Idempotency-Key": args.idempotency_key or str(uuid4())},
            json={
                "space_id": args.space,
                "title": args.title,
                "content": args.content,
                "tags": args.tag or [],
            },
        )
    if response.status_code != 201:
        print(f"Create failed ({response.status_code}): {response.text}")
        return 1
    _print(response.json())
    return 0


def execute_update(args: argparse.Namespace) -> int:
    with _client(_base_url(args), _api_key(args)) as client:
        response = client.put(
            f"/api/v1/knowledge/{args.item_id}",
            headers={"Idempotency-Key": args.idempotency_key or str(uuid4())},
            json={
                "expected_version": args.expected_version,
                "title": args.title,
                "content": args.content,
                "tags": args.tag or [],
            },
        )
    if response.status_code != 200:
        print(f"Update failed ({response.status_code}): {response.text}")
        return 1
    _print(response.json())
    return 0


def execute_upload(args: argparse.Namespace) -> int:
    path = args.file
    try:
        handle = open(path, "rb")
    except OSError as exc:
        print(f"Cannot read file: {exc}")
        return 1
    with handle, _client(_base_url(args), _api_key(args)) as client:
        response = client.post(
            "/api/v1/sources/upload",
            headers={"Idempotency-Key": args.idempotency_key or str(uuid4())},
            files={"file": (os.path.basename(path), handle)},
            data={"space_id": args.space, **({"display_name": args.display_name} if args.display_name else {})},
        )
    if response.status_code != 202:
        print(f"Upload failed ({response.status_code}): {response.text}")
        return 1
    _print(response.json())
    return 0


def execute_job(args: argparse.Namespace) -> int:
    with _client(_base_url(args), _api_key(args)) as client:
        if args.watch:
            import time

            deadline = time.monotonic() + args.timeout
            while True:
                response = client.get("/api/v1/ingestion-jobs", params={"space_id": args.space} if args.space else None)
                if response.status_code != 200:
                    print(f"Job status failed ({response.status_code}): {response.text}")
                    return 1
                jobs = [job for job in response.json()["items"] if job["id"] == args.job_id]
                if not jobs:
                    print(f"Job {args.job_id} is not visible to this key.")
                    return 1
                state = jobs[0]["state"]
                if state in ("succeeded", "failed", "cancelled") or time.monotonic() > deadline:
                    _print(jobs[0])
                    return 0 if state == "succeeded" else 1
                time.sleep(args.interval)
        response = client.get("/api/v1/ingestion-jobs", params={"space_id": args.space} if args.space else None)
    if response.status_code != 200:
        print(f"Job status failed ({response.status_code}): {response.text}")
        return 1
    items = response.json()["items"]
    if args.job_id:
        items = [job for job in items if job["id"] == args.job_id]
    _print(items)
    return 0


def execute_context(args: argparse.Namespace) -> int:
    with _client(_base_url(args), _api_key(args)) as client:
        response = client.post(
            "/api/v1/retrieval/search",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "query": args.query,
                "limit": args.limit,
                **({"space_ids": args.space.split(",")} if args.space else {}),
            },
        )
    if response.status_code != 200:
        print(f"Context failed ({response.status_code}): {response.text}")
        return 1
    package = response.json()
    lines = [f"# {hit['title']} ({hit['citation_uri'] or hit['canonical_id']})" for hit in package.get("hits", [])]
    lines += ["", *[hit.get("content_excerpt", "") for hit in package.get("hits", [])]]
    print("\n".join(lines).strip())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openknowledge")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--space", default=None)
    search.add_argument("--limit", type=int, default=10)

    create = commands.add_parser("create")
    create.add_argument("--space", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--content", required=True)
    create.add_argument("--tag", action="append", default=None)
    create.add_argument("--idempotency-key", default=None)

    update = commands.add_parser("update")
    update.add_argument("item_id")
    update.add_argument("--expected-version", type=int, required=True)
    update.add_argument("--title", required=True)
    update.add_argument("--content", required=True)
    update.add_argument("--tag", action="append", default=None)
    update.add_argument("--idempotency-key", default=None)

    upload = commands.add_parser("upload")
    upload.add_argument("--space", required=True)
    upload.add_argument("--file", required=True)
    upload.add_argument("--display-name", default=None)
    upload.add_argument("--idempotency-key", default=None)

    job = commands.add_parser("job")
    job.add_argument("--job-id", default=None)
    job.add_argument("--space", default=None)
    job.add_argument("--watch", action="store_true")
    job.add_argument("--timeout", type=float, default=300.0)
    job.add_argument("--interval", type=float, default=5.0)

    context = commands.add_parser("context")
    context.add_argument("query")
    context.add_argument("--space", default=None)
    context.add_argument("--limit", type=int, default=10)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "search": execute_search,
        "create": execute_create,
        "update": execute_update,
        "upload": execute_upload,
        "job": execute_job,
        "context": execute_context,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
