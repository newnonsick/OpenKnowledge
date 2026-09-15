import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.gateway.application.services.quota_service import quota_service_from_settings
from src.gateway.config import Settings, reset_runtime_settings, set_runtime_settings
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.quotas import QuotaMiddleware
from src.gateway.presentation.settings_context import SettingsContextMiddleware


def quota_limited_settings():
    return Settings(
        gateway={
            "environment": "test",
            "quota_requests_per_minute": 1,
            "quota_burst_requests": 1,
            "quota_concurrent_requests": 4,
        }
    )


def build_quota_app(active):
    app = FastAPI()
    app.state.settings = active
    register_exception_handlers(app)
    app.add_middleware(SettingsContextMiddleware)
    app.add_middleware(QuotaMiddleware)

    @app.middleware("http")
    async def probe_principal(request: Request, call_next):
        request.scope.setdefault("state", {})["principal"] = Principal(
            subject_id=request.headers.get("X-Test-Subject", "probe-member"),
            kind=PrincipalKind.API_KEY,
            system_role=SystemRole.MEMBER,
            scopes=frozenset({"spaces:read"}),
            credential_id=request.headers.get("X-Test-Credential", "probe-credential"),
        )
        return await call_next(request)

    @app.get("/quota-probe")
    async def quota_probe():
        return JSONResponse({"ok": True})

    return app


async def test_quota_middleware_returns_429_with_retry_after() -> None:
    active = quota_limited_settings()
    token = set_runtime_settings(active)
    service = quota_service_from_settings(active.gateway)
    app = build_quota_app(active)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://gateway.test",
        ) as client:
            assert (await client.get("/quota-probe")).status_code == 200
            assert (await client.get("/quota-probe")).status_code == 200
            throttled = await client.get("/quota-probe")
            assert throttled.status_code == 429
            assert "Retry-After" in throttled.headers
            assert int(throttled.headers["Retry-After"]) >= 1
            payload = throttled.json()
            assert payload["error"]["code"] == "quota_exceeded"
            assert payload["error"]["details"]["retry_after_seconds"] >= 1
            assert service.policy.requests_per_minute == 1
    finally:
        reset_runtime_settings(token)


async def test_quota_middleware_isolates_credentials() -> None:
    active = quota_limited_settings()
    token = set_runtime_settings(active)
    app = build_quota_app(active)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://gateway.test",
        ) as client:
            await client.get("/quota-probe", headers={"X-Test-Credential": "credential-a"})
            await client.get("/quota-probe", headers={"X-Test-Credential": "credential-a"})
            throttled = await client.get(
                "/quota-probe", headers={"X-Test-Credential": "credential-a"}
            )
            assert throttled.status_code == 429
            other = await client.get("/quota-probe", headers={"X-Test-Credential": "credential-b"})
            assert other.status_code == 200
    finally:
        reset_runtime_settings(token)


async def test_quota_middleware_ignores_unauthorized_space_for_burst() -> None:
    active = quota_limited_settings()
    token = set_runtime_settings(active)
    app = build_quota_app(active)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://gateway.test",
        ) as client:
            statuses = []
            for index in range(4):
                response = await client.get(
                    f"/quota-probe?space_id=invented-{index}",
                    headers={"X-Test-Credential": "rotating-credential"},
                )
                statuses.append(response.status_code)
            assert 429 in statuses
    finally:
        reset_runtime_settings(token)


async def test_quota_middleware_releases_slot_on_persistent_rejection(monkeypatch) -> None:
    from src.gateway.domain.exceptions import QuotaExceededException
    from src.gateway.presentation import quotas as quotas_module

    active = Settings(
        gateway={
            "environment": "test",
            "quota_requests_per_minute": 100,
            "quota_burst_requests": 100,
            "quota_concurrent_requests": 1,
        }
    )
    token = set_runtime_settings(active)
    state = {"reject": True}

    async def _reject_once(_service, _principal, _space_id):
        if state["reject"]:
            raise QuotaExceededException(60, quota="requests", limit=100)
        return None

    monkeypatch.setattr(quotas_module, "_check_persistent_window", _reject_once)
    app = build_quota_app(active)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://gateway.test",
        ) as client:
            headers = {"X-Test-Credential": "leak-credential"}
            first = await client.get("/quota-probe", headers=headers)
            assert first.status_code == 429
            assert first.json()["error"]["code"] == "quota_exceeded"
            state["reject"] = False
            second = await client.get("/quota-probe", headers=headers)
            assert second.status_code == 200
    finally:
        reset_runtime_settings(token)
