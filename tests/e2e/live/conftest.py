from __future__ import annotations

import os
import secrets
from typing import Any

import pytest

from tests.e2e.live._bootstrap import (
    BASE_URL,
    ADMIN_USERNAME,
    BROWSER_ORIGIN,
    LiveClient,
    cached_admin_bootstrap,
    clear_login_throttle,
    throttled_login,
)

pytestmark = pytest.mark.live_e2e


def _skip_without_live_server() -> None:
    if os.environ.get("OPENKNOWLEDGE_LIVE_E2E") != "1":
        pytest.skip("Live E2E disabled; set OPENKNOWLEDGE_LIVE_E2E=1")


@pytest.fixture(scope="session")
def admin_client() -> LiveClient:
    _skip_without_live_server()
    client, _state = cached_admin_bootstrap(BASE_URL, ADMIN_USERNAME)
    yield client
    client.close()


@pytest.fixture(scope="session")
def admin_state() -> dict[str, Any]:
    _skip_without_live_server()
    _client, state = cached_admin_bootstrap(BASE_URL, ADMIN_USERNAME)
    return state


@pytest.fixture(scope="session")
def api_key(admin_state: dict[str, Any]) -> str:
    _skip_without_live_server()
    return admin_state["api_key_secret"]


@pytest.fixture
def unique_prefix() -> str:
    return f"lv{secrets.token_hex(4)}"


@pytest.fixture
def member_factory(admin_client: LiveClient):
    created_members: list[dict[str, Any]] = []

    def _create(prefix: str, display_name: str | None = None) -> dict[str, Any]:
        username = f"{prefix}_{secrets.token_hex(4)}"
        response = admin_client.post(
            "/api/v1/members",
            json_body={"username": username, "display_name": display_name or username},
            idempotency_key=f"e2e-member-{secrets.token_hex(8)}",
        )
        assert response.status_code == 201, response.text
        info = response.json()
        temp_password = info["temporary_password"]

        member_client = LiveClient()
        login = throttled_login(member_client, username, temp_password)
        assert login.status_code == 200, login.text
        assert login.json()["requires_password_change"] is True
        new_password = f"E2e-{secrets.token_urlsafe(18)}!"
        change = member_client.post(
            "/api/v1/auth/password",
            json_body={"password": new_password, "confirmation": new_password},
        )
        assert change.status_code == 200, change.text
        change_payload = change.json()
        initial_key = change_payload.get("initial_api_key")
        info.update(
            {
                "username": username,
                "password": new_password,
                "member_id": change_payload["member_id"],
                "system_role": change_payload["system_role"],
                "client": member_client,
                "api_key_secret": initial_key["secret"] if initial_key else None,
            }
        )
        created_members.append(info)
        return info

    yield _create

    for info in created_members:
        try:
            info["client"].close()
        except Exception:
            pass


@pytest.fixture
def space_factory(admin_client: LiveClient):
    created_spaces: list[dict[str, Any]] = []

    def _create(name: str) -> dict[str, Any]:
        response = admin_client.post(
            "/api/v1/spaces",
            json_body={"name": name},
            idempotency_key=f"e2e-space-{secrets.token_hex(8)}",
        )
        assert response.status_code in (200, 201), response.text
        space = response.json()
        created_spaces.append(space)
        return space

    yield _create

    for space in created_spaces:
        try:
            admin_client.delete(
                f"/api/v1/spaces/{space['id']}",
                idempotency_key=f"e2e-space-del-{secrets.token_hex(8)}",
            )
        except Exception:
            pass
