from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, BinaryIO, Optional, Sequence
from uuid import uuid4

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
ENV_BASE_URL = "OPENKNOWLEDGE_URL"
ENV_API_KEY = "OPENKNOWLEDGE_API_KEY"


class OpenKnowledgeError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_text: str = "",
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_text = response_text
        self.payload = payload


class OpenKnowledgeClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolved_base = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        resolved_key = api_key or os.environ.get(ENV_API_KEY)
        if not resolved_key:
            raise OpenKnowledgeError("Provide an API key or set OPENKNOWLEDGE_API_KEY.")
        self._base_url = resolved_base
        self._client = httpx.Client(
            base_url=resolved_base,
            headers={"Authorization": f"Bearer {resolved_key}"},
            timeout=timeout,
            transport=transport,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenKnowledgeClient:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        expected_status: int,
        **kwargs: Any,
    ) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code != expected_status:
            payload: Any = None
            try:
                payload = response.json()
            except ValueError:
                payload = None
            raise OpenKnowledgeError(
                f"{operation} failed with status {response.status_code}.",
                status_code=response.status_code,
                response_text=response.text,
                payload=payload,
            )
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise OpenKnowledgeError(
                f"{operation} returned an unreadable payload.",
                status_code=response.status_code,
                response_text=response.text,
            ) from exc

    def search(
        self,
        query: str,
        *,
        space: str | None = None,
        limit: int = 10,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/retrieval/search",
            operation="Search",
            expected_status=200,
            headers={"Idempotency-Key": idempotency_key or str(uuid4())},
            json={
                "query": query,
                "limit": limit,
                **({"space_ids": space.split(",")} if space else {}),
            },
        )

    def create(
        self,
        space: str,
        title: str,
        content: str,
        *,
        tags: Sequence[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/knowledge",
            operation="Create",
            expected_status=201,
            headers={"Idempotency-Key": idempotency_key or str(uuid4())},
            json={
                "space_id": space,
                "title": title,
                "content": content,
                "tags": list(tags or []),
            },
        )

    def update(
        self,
        item_id: str,
        expected_version: int,
        title: str,
        content: str,
        *,
        tags: Sequence[str] | None = None,
        change_summary: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "expected_version": expected_version,
            "title": title,
            "content": content,
            "tags": list(tags or []),
        }
        if change_summary is not None:
            body["change_summary"] = change_summary
        return self._request(
            "PUT",
            f"/api/v1/knowledge/{item_id}",
            operation="Update",
            expected_status=200,
            headers={"Idempotency-Key": idempotency_key or str(uuid4())},
            json=body,
        )

    def upload(
        self,
        space: str,
        file: str | os.PathLike[str] | BinaryIO,
        *,
        filename: str | None = None,
        display_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        handle: BinaryIO
        resolved_name = filename
        should_close = False
        if isinstance(file, (str, os.PathLike)):
            resolved_name = resolved_name or os.path.basename(os.fspath(file))
            try:
                handle = open(file, "rb")
            except OSError as exc:
                raise OpenKnowledgeError(f"Cannot read file: {exc}") from exc
            should_close = True
        else:
            handle = file
            resolved_name = resolved_name or display_name or "upload.bin"
        try:
            return self._request(
                "POST",
                "/api/v1/sources/upload",
                operation="Upload",
                expected_status=202,
                headers={"Idempotency-Key": idempotency_key or str(uuid4())},
                files={"file": (resolved_name, handle)},
                data={"space_id": space, **({"display_name": display_name} if display_name else {})},
            )
        finally:
            if should_close:
                handle.close()

    def jobs(self, *, space: str | None = None, job_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v1/ingestion-jobs",
            operation="Job status",
            expected_status=200,
            params={"space_id": space} if space else None,
        )
        items = list(payload["items"])
        if job_id:
            items = [job for job in items if job["id"] == job_id]
        return items

    def watch_job(
        self,
        job_id: str,
        *,
        space: str | None = None,
        timeout: float = 300.0,
        interval: float = 5.0,
    ) -> dict[str, Any]:
        if not job_id:
            raise OpenKnowledgeError(f"Job {job_id} is not visible to this key.")
        deadline = time.monotonic() + timeout
        while True:
            items = self.jobs(space=space, job_id=job_id)
            if not items:
                raise OpenKnowledgeError(f"Job {job_id} is not visible to this key.")
            state = items[0]["state"]
            if state in ("succeeded", "failed", "cancelled") or time.monotonic() > deadline:
                return items[0]
            time.sleep(interval)

    def context(
        self,
        query: str,
        *,
        space: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/context/assemble",
            operation="Context",
            expected_status=200,
            json={
                "query": query,
                "max_sources": limit,
                **({"space_ids": space.split(",")} if space else {}),
            },
        )

    def fetch(self, item_id: str, revision_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/evidence/knowledge/{item_id}/revisions/{revision_id}",
            operation="Fetch",
            expected_status=200,
        )

    def resolve(self, citation_uri: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/evidence/resolve",
            operation="Resolve",
            expected_status=200,
            json={"citation_uri": citation_uri},
        )

    def archive(
        self,
        item_id: str,
        expected_version: int,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self._request(
            "DELETE",
            f"/api/v1/knowledge/{item_id}",
            operation="Archive",
            expected_status=204,
            headers={"Idempotency-Key": idempotency_key or str(uuid4())},
            params={"expected_version": expected_version},
        )
        return {"id": item_id, "archived": True}

    def quota(self, space: str = "global") -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/v1/quotas/usage",
            operation="Quota",
            expected_status=200,
            params={"space_id": space},
        )


def _sdk(args: argparse.Namespace) -> OpenKnowledgeClient:
    try:
        return OpenKnowledgeClient(base_url=args.base_url, api_key=args.api_key)
    except OpenKnowledgeError:
        raise SystemExit("Provide --api-key or set OPENKNOWLEDGE_API_KEY.")


def _failure(operation: str, exc: OpenKnowledgeError) -> int:
    if exc.status_code is None:
        print(exc.message)
    else:
        print(f"{operation} failed ({exc.status_code}): {exc.response_text}")
    return 1


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def execute_search(args: argparse.Namespace) -> int:
    with _sdk(args) as client:
        try:
            _print(client.search(args.query, space=args.space, limit=args.limit))
        except OpenKnowledgeError as exc:
            if exc.status_code is None:
                raise SystemExit("Provide --api-key or set OPENKNOWLEDGE_API_KEY.")
            return _failure("Search", exc)
    return 0


def execute_create(args: argparse.Namespace) -> int:
    with _sdk(args) as client:
        try:
            _print(
                client.create(
                    args.space,
                    args.title,
                    args.content,
                    tags=args.tag,
                    idempotency_key=args.idempotency_key,
                )
            )
        except OpenKnowledgeError as exc:
            return _failure("Create", exc)
    return 0


def execute_update(args: argparse.Namespace) -> int:
    with _sdk(args) as client:
        try:
            _print(
                client.update(
                    args.item_id,
                    args.expected_version,
                    args.title,
                    args.content,
                    tags=args.tag,
                    idempotency_key=args.idempotency_key,
                )
            )
        except OpenKnowledgeError as exc:
            return _failure("Update", exc)
    return 0


def execute_upload(args: argparse.Namespace) -> int:
    with _sdk(args) as client:
        try:
            _print(
                client.upload(
                    args.space,
                    args.file,
                    display_name=args.display_name,
                    idempotency_key=args.idempotency_key,
                )
            )
        except OpenKnowledgeError as exc:
            return _failure("Upload", exc)
    return 0


def execute_job(args: argparse.Namespace) -> int:
    with _sdk(args) as client:
        try:
            if args.watch:
                job = client.watch_job(
                    args.job_id,
                    space=args.space,
                    timeout=args.timeout,
                    interval=args.interval,
                )
                _print(job)
                return 0 if job["state"] == "succeeded" else 1
            _print(client.jobs(space=args.space, job_id=args.job_id))
        except OpenKnowledgeError as exc:
            return _failure("Job status", exc)
    return 0


def execute_context(args: argparse.Namespace) -> int:
    with _sdk(args) as client:
        try:
            package = client.context(args.query, space=args.space, limit=args.limit)
        except OpenKnowledgeError as exc:
            return _failure("Context", exc)
    lines = [
        f"# {snippet['title']} ({snippet['citation_uri']})"
        for snippet in package.get("snippets", [])
    ]
    lines += ["", *[snippet.get("snippet", "") for snippet in package.get("snippets", [])]]
    if package.get("omitted_reason"):
        lines += ["", package["omitted_reason"]]
    print("\n".join(lines).strip())
    return 0


def execute_fetch(args: argparse.Namespace) -> int:
    with _sdk(args) as client:
        try:
            _print(client.fetch(args.item_id, args.revision_id))
        except OpenKnowledgeError as exc:
            return _failure("Fetch", exc)
    return 0


def execute_resolve(args: argparse.Namespace) -> int:
    with _sdk(args) as client:
        try:
            _print(client.resolve(args.citation_uri))
        except OpenKnowledgeError as exc:
            return _failure("Resolve", exc)
    return 0


def execute_archive(args: argparse.Namespace) -> int:
    with _sdk(args) as client:
        try:
            _print(client.archive(args.item_id, args.expected_version, idempotency_key=args.idempotency_key))
        except OpenKnowledgeError as exc:
            return _failure("Archive", exc)
    return 0


def execute_quota(args: argparse.Namespace) -> int:
    with _sdk(args) as client:
        try:
            _print(client.quota(args.space))
        except OpenKnowledgeError as exc:
            return _failure("Quota", exc)
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

    fetch = commands.add_parser("fetch")
    fetch.add_argument("item_id")
    fetch.add_argument("revision_id")

    resolve = commands.add_parser("resolve")
    resolve.add_argument("citation_uri")

    archive = commands.add_parser("archive")
    archive.add_argument("item_id")
    archive.add_argument("--expected-version", type=int, required=True)
    archive.add_argument("--idempotency-key", default=None)

    quota = commands.add_parser("quota")
    quota.add_argument("--space", default="global")
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
        "fetch": execute_fetch,
        "resolve": execute_resolve,
        "archive": execute_archive,
        "quota": execute_quota,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
