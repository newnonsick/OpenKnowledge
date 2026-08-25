from __future__ import annotations

import secrets
import time

from tests.e2e.live.conftest import BASE_URL, LiveClient, pytestmark  # noqa: F401


def test_runtime_settings_draft_activate_history_rollback(admin_client):
    active = admin_client.get("/api/v1/settings")
    assert active.status_code == 200, active.text
    original_revision = active.json()["revision"]
    original_values = active.json()["values"]

    draft_payload = {
        "base_revision": original_revision,
        "reason": f"live e2e tuning {secrets.token_hex(4)}",
        "values": original_values | {"retrieval": original_values["retrieval"] | {"rrf_k": 90}},
    }
    draft = admin_client.post(
        "/api/v1/settings/drafts",
        json_body=draft_payload,
        idempotency_key=f"e2e-draft-{secrets.token_hex(8)}",
    )
    assert draft.status_code in (200, 201), draft.text
    draft_id = draft.json()["id"]

    activate = admin_client.post(
        f"/api/v1/settings/drafts/{draft_id}/activate",
        json_body={
            "expected_active_revision": original_revision,
            "reason": "live e2e activation",
        },
        idempotency_key=f"e2e-act-{secrets.token_hex(8)}",
    )
    assert activate.status_code == 200, activate.text

    after = admin_client.get("/api/v1/settings")
    assert after.json()["revision"] > original_revision
    assert after.json()["values"]["retrieval"]["rrf_k"] == 90
    new_revision = after.json()["revision"]

    history = admin_client.get("/api/v1/settings/history")
    assert history.status_code == 200, history.text
    revisions = [entry["revision"] for entry in history.json().get("items", history.json())]
    assert original_revision in revisions and new_revision in revisions

    stale_conflict = admin_client.post(
        f"/api/v1/settings/drafts/{draft_id}/activate",
        json_body={
            "expected_active_revision": original_revision,
            "reason": "stale activation must conflict",
        },
        idempotency_key=f"e2e-act2-{secrets.token_hex(8)}",
    )
    assert stale_conflict.status_code in (409, 400), stale_conflict.text

    rollback = admin_client.post(
        f"/api/v1/settings/rollback/{original_revision}",
        json_body={
            "expected_active_revision": new_revision,
            "reason": "live e2e rollback",
        },
        idempotency_key=f"e2e-rb-{secrets.token_hex(8)}",
    )
    assert rollback.status_code == 200, rollback.text

    restored = admin_client.get("/api/v1/settings")
    assert restored.json()["values"]["retrieval"]["rrf_k"] == original_values["retrieval"]["rrf_k"]


def test_operations_summary_and_audit_trail(admin_client, space_factory):
    space = space_factory(f"Audit Space {secrets.token_hex(4)}")

    summary = admin_client.get("/api/v1/operations/summary")
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["scope"] in {"accessible_spaces", "global"}
    assert "ingestion" in body and "settings_revision" in body

    time.sleep(0.5)
    audit = admin_client.get("/api/v1/audit-events", params={"page": 1, "page_size": 50})
    assert audit.status_code == 200, audit.text
    actions = [event.get("action") for event in audit.json()["items"]]
    assert any(action and action.startswith("space.") for action in actions), actions[:10]


def test_ai_tools_listing_and_direct_execution(admin_client):
    listing = admin_client.get("/api/v1/ai-tools")
    assert listing.status_code == 200, listing.text
    names = {tool["name"] for tool in listing.json()["items"]}
    assert {"spaces.list.v1", "knowledge.search.v1", "settings.propose.v1"} <= names

    executed = admin_client.post(
        "/api/v1/ai-tools/spaces.list.v1",
        json_body={"arguments": {"limit": 10}},
        idempotency_key=f"e2e-tool-{secrets.token_hex(8)}",
    )
    assert executed.status_code in (200, 201), executed.text
    payload = executed.json()
    if "status" in payload:
        assert payload["status"] == "executed"
        space_names = [space["name"] for space in payload["result"]["items"]]
    else:
        space_names = [space["name"] for space in payload["items"]]
    assert space_names


def test_ai_tool_proposal_requires_confirmation(admin_client):
    settings = admin_client.get("/api/v1/settings").json()

    proposed = admin_client.post(
        "/api/v1/ai-tools/settings.propose.v1",
        json_body={
            "arguments": {
                "base_revision": settings["revision"],
                "values": settings["values"],
                "reason": f"live e2e proposal {secrets.token_hex(4)}",
            }
        },
        idempotency_key=f"e2e-propose-{secrets.token_hex(8)}",
    )
    assert proposed.status_code == 202, proposed.text
    proposal = proposed.json()
    assert proposal["status"] == "confirmation_required"
    pending_action_id = proposal["pending_action_id"]

    pending_list = admin_client.get("/api/v1/ai-actions", params={"page": 1, "page_size": 50})
    assert pending_list.status_code == 200, pending_list.text
    pending_ids = [action["id"] for action in pending_list.json()["items"]]
    assert pending_action_id in pending_ids

    confirmed = admin_client.post(
        f"/api/v1/ai-actions/{pending_action_id}/confirm",
        json_body={},
        idempotency_key=f"e2e-confirm-{secrets.token_hex(8)}",
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "executed"


def test_api_key_lifecycle_revocation_blocks_chat(api_key: str):
    client = LiveClient(BASE_URL)
    try:
        me = client.get("/api/v1/me")
        member_id = None
        if me.status_code == 200:
            member_id = me.json().get("member_id") or me.json().get("id")

        from tests.e2e.live._bootstrap import cached_admin_bootstrap

        admin, _state = cached_admin_bootstrap()
        created = admin.post(
            "/api/v1/api-keys",
            json_body={
                "name": "revocable-e2e-key",
                "scopes": ["chat:write"],
            },
            idempotency_key=f"e2e-revoke-key-{secrets.token_hex(8)}",
        )
        assert created.status_code == 201, created.text
        key_body = created.json()
        secret = key_body["secret"]
        key_id = key_body["id"]

        probe = client.get("/v1/models", bearer=secret)
        assert probe.status_code == 200, probe.text

        revoked = admin.delete(
            f"/api/v1/api-keys/{key_id}",
            idempotency_key=f"e2e-revoke-del-{secrets.token_hex(8)}",
        )
        assert revoked.status_code == 204, revoked.text

        blocked = client.get("/v1/models", bearer=secret)
        assert blocked.status_code == 401, blocked.text
    finally:
        client.close()


def test_pagination_numeric_page_walk(admin_client):
    first_page = admin_client.get("/api/v1/members", params={"page": 1, "page_size": 3})
    assert first_page.status_code == 200
    page_body = first_page.json()
    if page_body["total_pages"] < 2:
        return
    second_page = admin_client.get("/api/v1/members", params={"page": 2, "page_size": 3})
    assert second_page.status_code == 200
    first_usernames = {item["username"] for item in page_body["items"]}
    second_usernames = {item["username"] for item in second_page.json()["items"]}
    assert not (first_usernames & second_usernames)
