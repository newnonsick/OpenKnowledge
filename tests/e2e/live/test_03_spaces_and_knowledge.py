from __future__ import annotations

import secrets

from tests.e2e.live.conftest import LiveClient, pytestmark, throttled_login  # noqa: F401


def test_member_administration_full_cycle(admin_client, unique_prefix):
    username = f"{unique_prefix}_member_{secrets.token_hex(3)}"

    created = admin_client.post(
        "/api/v1/members",
        json_body={"username": username, "display_name": "Member Full Cycle"},
        idempotency_key=f"e2e-mc-{secrets.token_hex(8)}",
    )
    assert created.status_code == 201, created.text
    body = created.json()
    member_id = body["id"]
    temp_password = body["temporary_password"]
    assert body["requires_password_change"] is True
    assert temp_password

    listing = admin_client.get("/api/v1/members", params={"limit": 100})
    assert listing.status_code == 200, listing.text
    usernames = [item["username"] for item in listing.json()["items"]]
    assert username in usernames

    suspended = admin_client.patch(
        f"/api/v1/members/{member_id}",
        json_body={
            "display_name": "Member Full Cycle",
            "status": "disabled",
            "system_role": "member",
        },
        idempotency_key=f"e2e-ms-{secrets.token_hex(8)}",
    )
    assert suspended.status_code == 200, suspended.text

    login_attempt = LiveClient()
    try:
        denied = throttled_login(login_attempt, username, temp_password)
        assert denied.status_code == 401, denied.text
    finally:
        login_attempt.close()

    reactivated = admin_client.patch(
        f"/api/v1/members/{member_id}",
        json_body={
            "display_name": "Member Full Cycle v2",
            "status": "active",
            "system_role": "member",
        },
        idempotency_key=f"e2e-mr-{secrets.token_hex(8)}",
    )
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["display_name"] == "Member Full Cycle v2"

    reset = admin_client.post(
        f"/api/v1/members/{member_id}/password-reset",
        idempotency_key=f"e2e-mp-{secrets.token_hex(8)}",
    )
    assert reset.status_code == 200, reset.text
    new_temp = reset.json()["temporary_password"]

    session_client = LiveClient()
    try:
        ok = throttled_login(session_client, username, new_temp)
        assert ok.status_code == 200, ok.text
        assert ok.json()["requires_password_change"] is True
    finally:
        session_client.close()


def test_space_lifecycle_and_membership_roles(admin_client, member_factory, space_factory):
    editor = member_factory("lved")
    reader = member_factory("lvrd")

    space = space_factory(f"Live Space {secrets.token_hex(4)}")
    space_id = space["id"]
    assert space["role"] == "owner"

    for candidate in (editor, reader):
        candidates = admin_client.get(
            f"/api/v1/spaces/{space_id}/member-candidates",
            params={"q": candidate["username"]},
        )
        assert candidates.status_code == 200, candidates.text
        candidate_ids = [item["member_id"] for item in candidates.json()["items"]]
        assert candidate["member_id"] in candidate_ids

    add_editor = admin_client.put(
        f"/api/v1/spaces/{space_id}/members/{editor['member_id']}",
        json_body={"role": "editor"},
        idempotency_key=f"e2e-sm-{secrets.token_hex(8)}",
    )
    assert add_editor.status_code == 200, add_editor.text

    add_reader = admin_client.put(
        f"/api/v1/spaces/{space_id}/members/{reader['member_id']}",
        json_body={"role": "reader"},
        idempotency_key=f"e2e-sm-{secrets.token_hex(8)}",
    )
    assert add_reader.status_code == 200, add_reader.text

    members_view = admin_client.get(f"/api/v1/spaces/{space_id}/members")
    assert members_view.status_code == 200, members_view.text
    roles = {
        item["member_id"]: item["role"]
        for item in members_view.json()["items"]
    }
    assert roles[editor["member_id"]] == "editor"
    assert roles[reader["member_id"]] == "reader"

    as_editor = editor["client"]
    create_ok = as_editor.post(
        "/api/v1/knowledge",
        json_body={
            "space_id": space_id,
            "title": "Editor note",
            "content": "Editors may write knowledge entries.",
            "tags": ["live-e2e"],
        },
        idempotency_key=f"e2e-kn-{secrets.token_hex(8)}",
    )
    assert create_ok.status_code == 201, create_ok.text
    item_id = create_ok.json()["id"]

    as_reader = reader["client"]
    denied_create = as_reader.post(
        "/api/v1/knowledge",
        json_body={
            "space_id": space_id,
            "title": "Reader note",
            "content": "Readers must not write.",
            "tags": [],
        },
        idempotency_key=f"e2e-kn-{secrets.token_hex(8)}",
    )
    assert denied_create.status_code in (403, 404), denied_create.text

    denied_update = as_reader.put(
        f"/api/v1/knowledge/{item_id}",
        json_body={
            "expected_version": 1,
            "title": "Hijack",
            "content": "Should not happen",
            "tags": [],
        },
        idempotency_key=f"e2e-kn-{secrets.token_hex(8)}",
    )
    assert denied_update.status_code in (403, 404), denied_update.text

    read_ok = as_reader.get(f"/api/v1/knowledge/{item_id}")
    assert read_ok.status_code == 200, read_ok.text

    remove_reader = admin_client.delete(
        f"/api/v1/spaces/{space_id}/members/{reader['member_id']}",
        idempotency_key=f"e2e-sm-{secrets.token_hex(8)}",
    )
    assert remove_reader.status_code in (200, 204), remove_reader.text

    blocked_read = as_reader.get(f"/api/v1/knowledge/{item_id}")
    assert blocked_read.status_code in (403, 404), blocked_read.text

    delete_space = admin_client.delete(
        f"/api/v1/spaces/{space_id}",
        idempotency_key=f"e2e-sd-{secrets.token_hex(8)}",
    )
    assert delete_space.status_code in (200, 204), delete_space.text


def test_knowledge_versioning_and_conflicts(admin_client, space_factory):
    space = space_factory(f"Versioning Space {secrets.token_hex(4)}")
    space_id = space["id"]
    marker = secrets.token_hex(6)

    create = admin_client.post(
        "/api/v1/knowledge",
        json_body={
            "space_id": space_id,
            "title": f"Deployment guide {marker}",
            "content": f"The production deploy command is `make deploy-{marker}`.",
            "tags": ["ops"],
        },
        idempotency_key=f"e2e-kn-{secrets.token_hex(8)}",
    )
    assert create.status_code == 201, create.text
    item = create.json()
    item_id = item["id"]
    assert item["version"] == 1
    assert item["content"].endswith(f"`make deploy-{marker}`.")

    stale = admin_client.put(
        f"/api/v1/knowledge/{item_id}",
        json_body={
            "expected_version": 99,
            "title": item["title"],
            "content": "stale write",
            "tags": [],
        },
        idempotency_key=f"e2e-kn-{secrets.token_hex(8)}",
    )
    assert stale.status_code == 409, stale.text

    update = admin_client.put(
        f"/api/v1/knowledge/{item_id}",
        json_body={
            "expected_version": 1,
            "title": f"Deployment guide v2 {marker}",
            "content": f"The new deploy command is `bin/rollout --{marker}`.",
            "tags": ["ops", "v2"],
            "change_summary": "Command changed",
        },
        idempotency_key=f"e2e-kn-{secrets.token_hex(8)}",
    )
    assert update.status_code == 200, update.text
    updated = update.json()
    assert updated["version"] == 2
    assert "bin/rollout" in updated["content"]

    detail = admin_client.get(f"/api/v1/knowledge/{item_id}")
    assert detail.status_code == 200
    assert detail.json()["version"] == 2

    listing = admin_client.get("/api/v1/knowledge", params={"space_ids": space_id})
    assert listing.status_code == 200, listing.text
    titles = [entry["title"] for entry in listing.json()["items"]]
    assert f"Deployment guide v2 {marker}" in titles

    bad_delete = admin_client.delete(
        f"/api/v1/knowledge/{item_id}",
        params={"expected_version": 1},
        idempotency_key=f"e2e-kd-{secrets.token_hex(8)}",
    )
    assert bad_delete.status_code == 409, bad_delete.text

    good_delete = admin_client.delete(
        f"/api/v1/knowledge/{item_id}",
        params={"expected_version": 2},
        idempotency_key=f"e2e-kd-{secrets.token_hex(8)}",
    )
    assert good_delete.status_code == 204, good_delete.text

    gone = admin_client.get(f"/api/v1/knowledge/{item_id}")
    assert gone.status_code in (403, 404), gone.text


def test_global_space_knowledge_visible_in_retrieval(admin_client):
    marker = secrets.token_hex(6)
    create = admin_client.post(
        "/api/v1/knowledge",
        json_body={
            "space_id": "global",
            "title": f"Shared onboarding secret {marker}",
            "content": (
                f"Company-wide fact {marker}: the internal API base URL is "
                f"https://internal.example-{marker}.invalid/v1 for all teams."
            ),
            "tags": ["onboarding"],
        },
        idempotency_key=f"e2e-kn-{secrets.token_hex(8)}",
    )
    assert create.status_code == 201, create.text

    search = admin_client.post(
        "/api/v1/retrieval/search",
        json_body={"query": f"What is the internal API base URL? {marker}", "limit": 10},
    )
    assert search.status_code == 200, search.text
    results = search.json()
    flat_text = str(results)
    assert marker in flat_text, "globally shared knowledge must be retrievable"
