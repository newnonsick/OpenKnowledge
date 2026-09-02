from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import pyotp

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PYTHON = str(PROJECT_ROOT / "venv" / "Scripts" / "python.exe")
if not Path(PYTHON).exists():
    PYTHON = sys.executable

BASE_URL = os.environ.get("OPENKNOWLEDGE_LIVE_BASE_URL", "http://127.0.0.1:8000")
ADMIN_USERNAME = os.environ.get("OPENKNOWLEDGE_LIVE_ADMIN_USERNAME", "admin")
BROWSER_ORIGIN = os.environ.get("OPENKNOWLEDGE_LIVE_ORIGIN", "http://localhost:3000")


class LiveClient:
    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=120.0)
        self.cookies: dict[str, str] = {}
        self.bearer: Optional[str] = None
        self.credentials: Optional[dict[str, str]] = None

    def absorb_cookies(self, response: httpx.Response) -> None:
        for raw_cookie in response.headers.get_list("set-cookie"):
            head = raw_cookie.split(";", 1)[0]
            if "=" not in head:
                continue
            name, _, value = head.partition("=")
            name = name.strip()
            value = value.strip()
            expires = any(
                part.strip().startswith("Max-Age=0") or part.strip().startswith("max-age=0")
                for part in raw_cookie.split(";")[1:]
            )
            if expires or value == "":
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value

    @property
    def csrf_token(self) -> Optional[str]:
        return self.cookies.get("openknowledge-csrf")

    def use_session(self, other: "LiveClient") -> None:
        self.cookies = dict(other.cookies)

    def clear_auth(self) -> None:
        self.cookies.clear()
        self.bearer = None

    def request(
        self,
        method: str,
        path: str,
        json_body: Any = None,
        *,
        bearer: Optional[str] = ...,
        files: Any = None,
        data: Any = None,
        params: Any = None,
        headers: Optional[dict[str, str]] = None,
        idempotency_key: Optional[str] = None,
        _allow_reauth: bool = True,
    ) -> httpx.Response:
        send_headers: dict[str, str] = {}
        if bearer is ...:
            bearer = self.bearer
        if bearer:
            send_headers["Authorization"] = f"Bearer {bearer}"
        elif self.cookies:
            send_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
                send_headers["Origin"] = BROWSER_ORIGIN
                if self.csrf_token:
                    send_headers["X-CSRF-Token"] = self.csrf_token
                else:
                    send_headers["X-CSRF-Token"] = "missing"
        if idempotency_key is not None:
            send_headers["Idempotency-Key"] = idempotency_key
        if headers:
            send_headers.update(headers)
        used_cookie_auth = not bearer and bool(self.cookies)
        response = self._client.request(
            method,
            path,
            json=json_body,
            files=files,
            data=data,
            params=params,
            headers=send_headers,
        )
        self.absorb_cookies(response)
        if (
            response.status_code == 401
            and _allow_reauth
            and used_cookie_auth
            and self.credentials
        ):
            if self._relogin():
                return self.request(
                    method,
                    path,
                    json_body=json_body,
                    files=files,
                    data=data,
                    params=params,
                    idempotency_key=idempotency_key,
                    _allow_reauth=False,
                )
        return response

    def _relogin(self) -> bool:
        creds = self.credentials or {}
        body: dict[str, Any] = {
            "username": creds.get("username", ""),
            "password": creds.get("password", ""),
        }
        secret = creds.get("totp_secret")
        if secret:
            body["totp_code"] = pyotp.TOTP(secret).now()
        response = self.post("/api/v1/auth/login", json_body=body, _allow_reauth=False)
        return response.status_code == 200

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json_body: Any = None, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, json_body=json_body, **kwargs)

    def put(self, path: str, json_body: Any = None, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", path, json_body=json_body, **kwargs)

    def patch(self, path: str, json_body: Any = None, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", path, json_body=json_body, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)

    def stream_sse(self, method: str, path: str, json_body: Any, *, bearer: Optional[str] = ...) -> httpx.Response:
        send_headers = {"Accept": "text/event-stream"}
        use_bearer = bearer if bearer is not ... else self.bearer
        if use_bearer:
            send_headers["Authorization"] = f"Bearer {use_bearer}"
        elif self.cookies:
            send_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return self._client.request(method, path, json=json_body, headers=send_headers)

    def close(self) -> None:
        self._client.close()


def recover_admin_password(username: str) -> str:
    result = subprocess.run(
        [PYTHON, "-m", "src.gateway.cli", "recover-super-admin", "--username", username, "--allow-secret-output"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    match = re.search(r"Temporary password: (.*)", result.stdout)
    if not match:
        raise AssertionError(f"recover-super-admin failed:\nstdout={result.stdout}\nstderr={result.stderr}")
    return match.group(1).strip()


def clear_login_throttle(*usernames: str) -> None:
    import asyncio
    import hashlib

    import asyncpg
    from dotenv import dotenv_values

    from src.gateway.domain.identity import normalize_username

    config = dotenv_values(PROJECT_ROOT / ".env")
    database_url = str(config.get("DATABASE_URL", "")).replace("postgresql+asyncpg://", "postgresql://")
    if not database_url:
        return
    digests = [hashlib.sha256(normalize_username(name).encode("utf-8")).hexdigest() for name in usernames]

    async def _clear() -> None:
        conn = await asyncpg.connect(database_url, timeout=20)
        try:
            await conn.execute(
                "DELETE FROM public.login_throttle_buckets WHERE bucket_type = 'account' AND bucket_key = ANY($1::text[])",
                digests,
            )
            await conn.execute("DELETE FROM public.login_throttle_buckets WHERE bucket_type IN ('ip', 'global')")
        finally:
            await conn.close()

    asyncio.run(_clear())


def throttled_login(client: LiveClient, username: str, password: str, **extra: Any) -> httpx.Response:
    body = {"username": username, "password": password, **extra}
    deadline = time.monotonic() + 300
    while True:
        response = client.post("/api/v1/auth/login", json_body=body)
        if response.status_code != 429 or time.monotonic() > deadline:
            return response
        retry_after = response.json()["error"]["details"]["retry_after_seconds"]
        time.sleep(min(retry_after + 1, 60))


def complete_admin_bootstrap(base_url: str, username: str) -> tuple[LiveClient, dict[str, Any]]:
    clear_login_throttle(username)
    temporary_password = recover_admin_password(username)

    client = LiveClient(base_url)
    login = throttled_login(client, username, temporary_password)
    assert login.status_code == 200, login.text
    payload = login.json()
    assert payload["requires_password_change"] is True

    new_password = f"E2e-{secrets.token_urlsafe(18)}!"
    change = client.post(
        "/api/v1/auth/password",
        json_body={"password": new_password, "confirmation": new_password},
    )
    assert change.status_code == 200, change.text
    change_payload = change.json()
    assert change_payload["requires_mfa_enrollment"] is True

    enroll = client.post("/api/v1/auth/mfa/totp/enroll", json_body={})
    assert enroll.status_code == 200, enroll.text
    factor_id = enroll.json()["factor_id"]
    secret = enroll.json()["secret"]

    totp = pyotp.TOTP(secret)
    code = totp.now()
    confirm = client.post(
        "/api/v1/auth/mfa/totp/confirm",
        json_body={"factor_id": factor_id, "code": code},
    )
    if confirm.status_code != 200:
        time.sleep(3)
        confirm = client.post(
            "/api/v1/auth/mfa/totp/confirm",
            json_body={"factor_id": factor_id, "code": totp.now()},
        )
    assert confirm.status_code == 200, confirm.text
    confirmed = confirm.json()
    assert confirmed["recovery_codes"], "recovery codes must be issued"

    created_key = client.post(
        "/api/v1/api-keys",
        json_body={
            "name": "live-e2e-key",
            "scopes": ["chat:write", "knowledge:read", "knowledge:write"],
        },
        idempotency_key=f"e2e-bootstrap-key-{secrets.token_hex(8)}",
    )
    assert created_key.status_code == 201, created_key.text
    key_body = created_key.json()
    assert key_body["secret"].startswith("openknowledge_v")

    state = {
        "username": username,
        "password": new_password,
        "temporary_password": temporary_password,
        "member_id": confirmed["member_id"],
        "recovery_codes": confirmed["recovery_codes"],
        "api_key_id": key_body["id"],
        "api_key_secret": key_body["secret"],
        "totp_secret": secret,
    }
    client.bearer = None
    client.credentials = {
        "username": username,
        "password": new_password,
        "totp_secret": secret,
    }
    return client, state


_BOOTSTRAP_CACHE: dict[str, tuple[LiveClient, dict[str, Any]]] = {}


def cached_admin_bootstrap(base_url: str = BASE_URL, username: str = ADMIN_USERNAME) -> tuple[LiveClient, dict[str, Any]]:
    cache_key = f"{base_url}|{username}"
    if cache_key not in _BOOTSTRAP_CACHE:
        _BOOTSTRAP_CACHE[cache_key] = complete_admin_bootstrap(base_url, username)
    return _BOOTSTRAP_CACHE[cache_key]
