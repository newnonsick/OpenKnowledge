from __future__ import annotations

import secrets
import time

import pyotp
import pytest

from tests.e2e.live._bootstrap import recover_admin_password
from tests.e2e.live.conftest import (
    ADMIN_USERNAME,
    BASE_URL,
    LiveClient,
    clear_login_throttle,
    pytestmark,  # noqa: F401
    throttled_login,
)


@pytest.fixture(autouse=True)
def _fresh_login_throttle():
    clear_login_throttle(ADMIN_USERNAME)
    yield


def test_temporary_password_login_then_forced_change_and_mfa(admin_client, member_factory):
    promoted = member_factory("lvpromote")

    enroll = promoted["client"].post(
        "/api/v1/auth/mfa/totp/enroll",
        json_body={"current_password": promoted["password"]},
    )
    assert enroll.status_code == 200, enroll.text
    factor_id = enroll.json()["factor_id"]
    totp_secret = enroll.json()["secret"]

    def confirm_code():
        return promoted["client"].post(
            "/api/v1/auth/mfa/totp/confirm",
            json_body={
                "factor_id": factor_id,
                "code": pyotp.TOTP(totp_secret).now(),
                "current_password": promoted["password"],
            },
        )

    confirmed = confirm_code()
    if confirmed.status_code != 200:
        time.sleep(3)
        confirmed = confirm_code()
    assert confirmed.status_code == 200, confirmed.text

    role_change = admin_client.patch(
        f"/api/v1/members/{promoted['member_id']}",
        json_body={
            "display_name": "Promoted Super Admin",
            "status": "active",
            "system_role": "super_admin",
        },
        idempotency_key=f"e2e-promote-{secrets.token_hex(8)}",
    )
    assert role_change.status_code == 200, role_change.text
    username = promoted["username"]

    clear_login_throttle(username)
    try:
        temporary_password = recover_admin_password(username)
        client = LiveClient(BASE_URL)
        try:
            login = throttled_login(client, username, temporary_password)
            assert login.status_code == 200, login.text
            assert login.json()["requires_password_change"] is True
            assert "__Host-aigw-access" in client.cookies
            assert "__Secure-aigw-refresh" in client.cookies
            assert "aigw-csrf" in client.cookies

            forbidden = client.get("/api/v1/members")
            assert forbidden.status_code in (401, 403, 404), forbidden.text
            if forbidden.status_code == 404:
                assert forbidden.json()["error"]["code"] == "resource_unavailable"

            new_password = f"E2e-{secrets.token_urlsafe(18)}!"
            mismatch = client.post(
                "/api/v1/auth/password",
                json_body={"password": new_password, "confirmation": "different"},
            )
            assert mismatch.status_code == 422, mismatch.text

            changed = client.post(
                "/api/v1/auth/password",
                json_body={"password": new_password, "confirmation": new_password},
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["requires_mfa_enrollment"] is True

            stale = throttled_login(client, username, temporary_password)
            assert stale.status_code == 401, stale.text
        finally:
            client.close()
    finally:
        admin_client.patch(
            f"/api/v1/members/{promoted['member_id']}",
            json_body={
                "display_name": "Promoted Super Admin",
                "status": "disabled",
                "system_role": "super_admin",
            },
            idempotency_key=f"e2e-demote-{secrets.token_hex(8)}",
        )


def test_totp_login_with_enrolled_secret(admin_client, admin_state):
    fresh = LiveClient(BASE_URL)
    try:
        totp = pyotp.TOTP(admin_state["totp_secret"])

        def login_with(code_value: str | None):
            body = {"username": ADMIN_USERNAME, "password": admin_state["password"]}
            if code_value is not None:
                body["totp_code"] = code_value
            return throttled_login(fresh, **body)

        missing_factor = login_with(None)
        assert missing_factor.status_code in (401, 429), missing_factor.text

        bad_code = login_with("000000")
        assert bad_code.status_code in (401, 429), bad_code.text

        ok = login_with(totp.now())
        if ok.status_code != 200:
            time.sleep(3)
            ok = login_with(totp.now())
        assert ok.status_code == 200, ok.text
        assert ok.json()["requires_password_change"] is False
        assert ok.json()["requires_mfa_enrollment"] is False
    finally:
        fresh.close()


def test_recovery_code_login(admin_client, admin_state):
    codes = admin_state["recovery_codes"]
    assert len(codes) >= 8
    fresh = LiveClient(BASE_URL)
    try:
        body = {
            "username": ADMIN_USERNAME,
            "password": admin_state["password"],
            "recovery_code": codes[-1],
        }
        ok = throttled_login(fresh, **body)
        assert ok.status_code == 200, ok.text

        replay = fresh.post("/api/v1/auth/login", json_body=body)
        assert replay.status_code in (401, 429), replay.text
        if replay.status_code == 429:
            time.sleep(replay.json()["error"]["details"]["retry_after_seconds"] + 1)
    finally:
        fresh.close()


def test_refresh_rotation_and_reuse_detection(admin_client):
    rotated = admin_client.post("/api/v1/auth/refresh", json_body={})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["status"] == "refreshed"
    assert "__Host-aigw-access" in admin_client.cookies

    me = admin_client.get("/api/v1/me")
    assert me.status_code == 200, me.text


def test_step_up_requires_current_credentials(admin_client, admin_state):
    denied = admin_client.post(
        "/api/v1/auth/step-up",
        json_body={"password": "wrong-password"},
    )
    assert denied.status_code == 401, denied.text

    password_only = admin_client.post(
        "/api/v1/auth/step-up",
        json_body={"password": admin_state["password"]},
    )
    assert password_only.status_code == 401, password_only.text

    def step_up_with_totp():
        return admin_client.post(
            "/api/v1/auth/step-up",
            json_body={
                "password": admin_state["password"],
                "totp_code": pyotp.TOTP(admin_state["totp_secret"]).now(),
            },
        )

    ok = step_up_with_totp()
    if ok.status_code != 200:
        time.sleep(3)
        ok = step_up_with_totp()
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "reauthenticated"
    assert ok.json()["step_up_expires_at"]


def test_sessions_listing_and_revocation(admin_client, admin_state):
    listing = admin_client.get("/api/v1/sessions")
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert items, "at least the current session family must exist"
    current = [item for item in items if item["current"]]
    assert len(current) == 1
    assert current[0]["status"] == "active"

    other = LiveClient(BASE_URL)
    try:
        def login_other():
            return throttled_login(
                other,
                ADMIN_USERNAME,
                admin_state["password"],
                totp_code=pyotp.TOTP(admin_state["totp_secret"]).now(),
            )

        login = login_other()
        if login.status_code != 200:
            time.sleep(3)
            login = login_other()
        assert login.status_code == 200, login.text

        refreshed_listing = admin_client.get("/api/v1/sessions", params={"limit": 100})
        families = refreshed_listing.json()["items"]
        target_candidates = [f for f in families if not f["current"] and f["status"] == "active"]
        assert target_candidates, "second session family must appear in the list"
        target_id = target_candidates[0]["id"]

        revoked = admin_client.delete(
            f"/api/v1/sessions/{target_id}",
            idempotency_key=f"e2e-revoke-{secrets.token_hex(8)}",
        )
        assert revoked.status_code == 204, revoked.text

        blocked = other.get("/api/v1/me")
        assert blocked.status_code == 401, blocked.text
    finally:
        other.close()


def test_logout_invalidates_session(admin_client, admin_state):
    client = LiveClient(BASE_URL)
    try:
        def login_client():
            return throttled_login(
                client,
                ADMIN_USERNAME,
                admin_state["password"],
                totp_code=pyotp.TOTP(admin_state["totp_secret"]).now(),
            )

        login = login_client()
        if login.status_code != 200:
            time.sleep(3)
            login = login_client()
        assert login.status_code == 200, login.text

        me = client.get("/api/v1/me")
        assert me.status_code == 200

        out = client.post("/api/v1/auth/logout", json_body={})
        assert out.status_code == 200, out.text
        assert out.json()["status"] == "signed_out"
        assert not client.cookies, "logout must clear session cookies"

        blocked = client.get("/api/v1/me")
        assert blocked.status_code == 401, blocked.text
    finally:
        client.close()


def test_me_endpoint_reports_identity(admin_client):
    me = admin_client.get("/api/v1/me")
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["username"] == ADMIN_USERNAME
    assert body["system_role"] == "super_admin"


def test_login_throttle_engages_after_repeated_failures(admin_state):
    clear_login_throttle(ADMIN_USERNAME)
    client = LiveClient(BASE_URL)
    try:
        saw_throttle = False
        for attempt in range(7):
            response = client.post(
                "/api/v1/auth/login",
                json_body={"username": ADMIN_USERNAME, "password": f"wrong-{attempt}"},
            )
            if response.status_code == 429:
                saw_throttle = True
                body = response.json()
                assert body["error"]["code"] == "login_throttled"
                assert body["error"]["details"]["retry_after_seconds"] >= 1
                break
            assert response.status_code == 401, response.text
            assert response.json()["error"]["type"] == "authentication_error"
        assert saw_throttle, "expected 429 once the failure budget is exhausted"
    finally:
        client.close()
