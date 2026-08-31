from datetime import datetime, timezone
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

from src.gateway.application.security.tokens import OpaqueTokenCodec
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MFAFactorModel, MemberModel, PasswordCredentialModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.routers.management import router
from src.gateway.presentation.routers import management as management_module
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


def principal(member_id, system_role=SystemRole.MEMBER):
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=system_role,
        scopes=frozenset({"*"}),
    )


async def test_management_resources_enforce_membership_and_one_time_secret_boundaries(tmp_path, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    admin_id = uuid4()
    member_id = uuid4()
    repair_target_id = uuid4()
    mfa_key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        gateway={
            "environment": "test",
            "public_base_url": "https://gateway.test",
            "api_key_peppers": {1: "p" * 32},
            "active_api_key_pepper_version": 1,
            "mfa_encryption_keys": {1: mfa_key},
            "active_mfa_encryption_key_version": 1,
            "storage_dir": str(tmp_path / "storage"),
        }
    )

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(
                        id=admin_id,
                        username="admin",
                        username_normalized="admin",
                        display_name="Admin",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.SUPER_ADMIN.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=member_id,
                        username="member",
                        username_normalized="member",
                        display_name="Member",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=repair_target_id,
                        username="repair-target",
                        username_normalized="repair-target",
                        display_name="Repair Target",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    Workspace(id="global", name="Family Shared", created_by_member_id=admin_id),
                    Workspace(id="repair-space", name="Private repair space", created_by_member_id=member_id),
                    EmbeddingGenerationModel(
                        id=uuid4(),
                        purpose="retrieval",
                        model_id="offline-test-generation",
                        dimensions=1024,
                        status="active",
                        activated_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    MFAFactorModel(
                        id=uuid4(),
                        member_id=member_id,
                        factor_type="totp",
                        secret_ciphertext=b"confirmed-factor",
                        encryption_key_version=1,
                        confirmed_at=now,
                    ),
                    MFAFactorModel(
                        id=uuid4(),
                        member_id=repair_target_id,
                        factor_type="totp",
                        secret_ciphertext=b"retired-factor",
                        encryption_key_version=1,
                        confirmed_at=now,
                        retired_at=now,
                    ),
                    SpaceMembershipModel(
                        id=uuid4(),
                        space_id="global",
                        member_id=admin_id,
                        role=SpaceRole.OWNER.value,
                    ),
                    SpaceMembershipModel(
                        id=uuid4(),
                        space_id="global",
                        member_id=member_id,
                        role=SpaceRole.EDITOR.value,
                    ),
                    SpaceMembershipModel(
                        id=uuid4(),
                        space_id="repair-space",
                        member_id=member_id,
                        role=SpaceRole.OWNER.value,
                    ),
                ]
            )
            admin_session = await SessionService(session).issue(
                principal(admin_id, SystemRole.SUPER_ADMIN),
                now=now,
                step_up_at=now,
            )
            member_session = await SessionService(session).issue(
                principal(member_id),
                now=now,
                step_up_at=now,
            )
            other_member_session = await SessionService(session).issue(
                principal(member_id),
                now=now,
            )

        app = FastAPI()
        app.state.settings = settings
        register_exception_handlers(app)
        app.add_middleware(
            APIKeyAuthMiddleware,
            allowed_keys=[],
            api_key_peppers={1: "p" * 32},
            session_factory=factory,
        )
        app.add_middleware(SettingsContextMiddleware)
        app.include_router(router)
        set_session_factory(factory)
        try:
            class StubEmbeddingClient:
                async def embed_query(self, query):
                    return [0.1] * 1024

            monkeypatch.setattr(
                management_module,
                "HTTPEmbeddingClient",
                lambda: StubEmbeddingClient(),
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-aigw-access": member_session.access_token.reveal()},
            ) as member_client:
                member_headers = {
                    "Origin": "https://gateway.test",
                    "X-CSRF-Token": member_session.csrf_token.reveal(),
                }
                me = await member_client.get("/api/v1/me")
                assert me.status_code == 200
                assert me.json()["id"] == str(member_id)
                assert me.json()["mfa_enabled"] is True

                spaces = await member_client.get("/api/v1/spaces", params={"page": 1, "page_size": 20})
                assert spaces.status_code == 200
                assert [(item["id"], item["role"]) for item in spaces.json()["items"]] == [
                    ("global", "editor"),
                    ("repair-space", "owner"),
                ]
                denied_admin_spaces = await member_client.get("/api/v1/admin/spaces")
                assert denied_admin_spaces.status_code == 404

                created_space = await member_client.post(
                    "/api/v1/spaces",
                    headers={**member_headers, "Idempotency-Key": "create-private-space"},
                    json={"name": "Research"},
                )
                assert created_space.status_code == 201
                private_space_id = created_space.json()["id"]
                assert created_space.json()["role"] == "owner"

                replay = await member_client.post(
                    "/api/v1/spaces",
                    headers={**member_headers, "Idempotency-Key": "create-private-space"},
                    json={"name": "Research"},
                )
                assert replay.status_code == 201
                assert replay.json()["id"] == private_space_id

                candidates = await member_client.get(
                    f"/api/v1/spaces/{private_space_id}/member-candidates"
                )
                assert candidates.status_code == 200
                assert {
                    item["username"] for item in candidates.json()["items"]
                } == {"admin", "repair-target"}

                membership = await member_client.put(
                    f"/api/v1/spaces/{private_space_id}/members/{admin_id}",
                    headers={**member_headers, "Idempotency-Key": "add-admin-reader"},
                    json={"role": "reader"},
                )
                assert membership.status_code == 200
                members = await member_client.get(f"/api/v1/spaces/{private_space_id}/members")
                assert members.status_code == 200
                assert {item["role"] for item in members.json()["items"]} == {"owner", "reader"}

                removed_membership = await member_client.delete(
                    f"/api/v1/spaces/{private_space_id}/members/{admin_id}",
                    headers={**member_headers, "Idempotency-Key": "remove-admin-reader"},
                )
                assert removed_membership.status_code == 204
                members_after_removal = await member_client.get(
                    f"/api/v1/spaces/{private_space_id}/members"
                )
                assert [item["member_id"] for item in members_after_removal.json()["items"]] == [
                    str(member_id)
                ]

                created_knowledge = await member_client.post(
                    "/api/v1/knowledge",
                    headers={**member_headers, "Idempotency-Key": "create-family-note"},
                    json={
                        "space_id": "global",
                        "title": "Emergency contacts",
                        "content": "Call the family coordinator before the building manager.",
                        "tags": ["home", "contact"],
                    },
                )
                assert created_knowledge.status_code == 201
                knowledge_id = created_knowledge.json()["id"]
                knowledge = await member_client.get("/api/v1/knowledge?space_id=global")
                assert knowledge.status_code == 200
                assert knowledge.json()["items"][0]["id"] == knowledge_id

                search = await member_client.post(
                    "/api/v1/retrieval/search",
                    headers=member_headers,
                    json={"query": "building manager", "space_ids": ["global"], "limit": 10},
                )
                assert search.status_code == 200
                assert search.json()["hits"][0]["canonical_id"] == knowledge_id
                assert search.json()["health"]["semantic_status"] == "active"

                updated_knowledge = await member_client.put(
                    f"/api/v1/knowledge/{knowledge_id}",
                    headers={**member_headers, "Idempotency-Key": "update-family-note"},
                    json={
                        "expected_version": 1,
                        "title": "Emergency contacts",
                        "content": "Call the family coordinator, then the building manager.",
                        "tags": ["home", "contact"],
                        "change_summary": "Clarified call order",
                    },
                )
                assert updated_knowledge.status_code == 200
                assert updated_knowledge.json()["version"] == 2
                detail = await member_client.get(f"/api/v1/knowledge/{knowledge_id}")
                assert detail.status_code == 200
                assert detail.json()["content"].startswith("Call the family coordinator")

                upload = await member_client.post(
                    "/api/v1/sources/upload",
                    headers={**member_headers, "Idempotency-Key": "upload-family-source"},
                    data={"space_id": "global", "display_name": "Family procedures"},
                    files={"file": ("procedures.txt", b"Turn off the water valve before repairs.", "text/plain")},
                )
                assert upload.status_code == 202
                assert upload.json()["job_state"] == "queued"
                second_upload = await member_client.post(
                    "/api/v1/sources/upload",
                    headers={**member_headers, "Idempotency-Key": "upload-second-source"},
                    data={"space_id": "global", "display_name": "Valve label"},
                    files={"file": ("label.txt", b"Blue valve", "text/plain")},
                )
                assert second_upload.status_code == 202
                sources = await member_client.get(
                    "/api/v1/sources",
                    params={"space_id": "global", "page": 1, "page_size": 1},
                )
                assert sources.status_code == 200
                next_sources = await member_client.get(
                    "/api/v1/sources",
                    params={"space_id": "global", "page": 2, "page_size": 1},
                )
                assert {sources.json()["items"][0]["id"], next_sources.json()["items"][0]["id"]} == {
                    upload.json()["document_id"], second_upload.json()["document_id"]
                }
                jobs = await member_client.get(
                    "/api/v1/ingestion-jobs",
                    params={"space_id": "global", "page": 1, "page_size": 1},
                )
                assert jobs.status_code == 200
                next_jobs = await member_client.get(
                    "/api/v1/ingestion-jobs",
                    params={"space_id": "global", "page": 2, "page_size": 1},
                )
                assert {jobs.json()["items"][0]["id"], next_jobs.json()["items"][0]["id"]} == {
                    upload.json()["job_id"], second_upload.json()["job_id"]
                }
                operations = await member_client.get("/api/v1/operations/summary")
                assert operations.status_code == 200
                assert operations.json()["scope"] == "accessible_spaces"
                assert operations.json()["ingestion"]["queued"] == 2
                assert operations.json()["storage"]["referenced_bytes"] == len(b"Turn off the water valve before repairs.Blue valve")
                assert operations.json()["retrieval"]["embedding_generation_active"] is True
                assert operations.json()["settings_revision"] == 0
                cancelled_job = await member_client.post(
                    f"/api/v1/ingestion-jobs/{upload.json()['job_id']}/cancel",
                    headers={**member_headers, "Idempotency-Key": "cancel-family-source"},
                )
                assert cancelled_job.status_code == 200
                assert cancelled_job.json()["state"] == "cancellation_requested"
                archived_source = await member_client.delete(
                    f"/api/v1/sources/{upload.json()['document_id']}?expected_revision=1",
                    headers={**member_headers, "Idempotency-Key": "archive-family-source"},
                )
                assert archived_source.status_code == 204
                sources_after_archive = await member_client.get("/api/v1/sources?space_id=global")
                assert [item["id"] for item in sources_after_archive.json()["items"]] == [
                    second_upload.json()["document_id"]
                ]

                deleted_knowledge = await member_client.delete(
                    f"/api/v1/knowledge/{knowledge_id}?expected_version=2",
                    headers={**member_headers, "Idempotency-Key": "delete-family-note"},
                )
                assert deleted_knowledge.status_code == 204
                missing_knowledge = await member_client.get(f"/api/v1/knowledge/{knowledge_id}")
                assert missing_knowledge.status_code == 404

                created_key = await member_client.post(
                    "/api/v1/api-keys",
                    headers={**member_headers, "Idempotency-Key": "create-laptop-key"},
                    json={"name": "Laptop", "scopes": ["knowledge:read", "chat:write"]},
                )
                assert created_key.status_code == 201
                raw_key = created_key.json()["secret"]
                assert raw_key.startswith("aigw_v1_")
                assert created_key.headers["Cache-Control"] == "no-store"

                keys = await member_client.get("/api/v1/api-keys")
                assert keys.status_code == 200
                assert keys.json()["items"][0]["name"] == "Laptop"
                assert "secret" not in keys.json()["items"][0]

                revoked_session = await member_client.delete(
                    f"/api/v1/sessions/{other_member_session.family_id}",
                    headers={**member_headers, "Idempotency-Key": "revoke-other-session"},
                )
                assert revoked_session.status_code == 204
                replayed_session_revoke = await member_client.delete(
                    f"/api/v1/sessions/{other_member_session.family_id}",
                    headers={**member_headers, "Idempotency-Key": "revoke-other-session"},
                )
                assert replayed_session_revoke.status_code == 204
                sessions_page = await member_client.get(
                    "/api/v1/sessions",
                    params={"page": 1, "page_size": 1},
                )
                sessions_next = await member_client.get(
                    "/api/v1/sessions",
                    params={"page": 2, "page_size": 1},
                )
                assert sessions_page.json()["items"][0]["id"] != sessions_next.json()["items"][0]["id"]

                archived = await member_client.delete(
                    f"/api/v1/spaces/{private_space_id}",
                    headers={**member_headers, "Idempotency-Key": "archive-private-space"},
                )
                assert archived.status_code == 204

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-aigw-access": admin_session.access_token.reveal()},
            ) as admin_client:
                admin_headers = {
                    "Origin": "https://gateway.test",
                    "X-CSRF-Token": admin_session.csrf_token.reveal(),
                    "Idempotency-Key": "create-new-member",
                }
                created_member = await admin_client.post(
                    "/api/v1/members",
                    headers=admin_headers,
                    json={"username": "new-member", "display_name": "New Member"},
                )
                assert created_member.status_code == 201
                temporary_password = created_member.json()["temporary_password"]
                assert temporary_password
                assert created_member.headers["Cache-Control"] == "no-store"
                member_list = await admin_client.get("/api/v1/members", params={"page": 1, "page_size": 50})
                assert member_list.status_code == 200
                assert "temporary_password" not in str(member_list.json())
                assert any(item["username"] == "new-member" for item in member_list.json()["items"])
                members_by_id = {item["id"]: item for item in member_list.json()["items"]}
                assert members_by_id[str(member_id)]["mfa_enabled"] is True
                assert members_by_id[str(repair_target_id)]["mfa_enabled"] is False
                created_member_id = created_member.json()["id"]
                admin_spaces = await admin_client.get("/api/v1/admin/spaces", params={"page": 1, "page_size": 100})
                assert admin_spaces.status_code == 200
                repair_space = next(
                    item for item in admin_spaces.json()["items"] if item["id"] == "repair-space"
                )
                assert repair_space["owner_member_id"] == str(member_id)
                assert repair_space["owner_username"] == "member"
                repaired_ownership = await admin_client.put(
                    "/api/v1/admin/spaces/repair-space/ownership",
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": admin_session.csrf_token.reveal(),
                        "Idempotency-Key": "repair-space-owner",
                    },
                    json={
                        "target_member_id": str(repair_target_id),
                        "expected_revision": repair_space["revision"],
                        "reason": "Restore ownership after account recovery",
                    },
                )
                assert repaired_ownership.status_code == 200
                assert repaired_ownership.json()["member_id"] == str(repair_target_id)
                reset_member = await admin_client.post(
                    f"/api/v1/members/{created_member_id}/password-reset",
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": admin_session.csrf_token.reveal(),
                        "Idempotency-Key": "reset-new-member",
                    },
                )
                assert reset_member.status_code == 200
                reset_password = reset_member.json()["temporary_password"]
                assert reset_password and reset_password != temporary_password
                assert reset_member.headers["Cache-Control"] == "no-store"
                disabled_member = await admin_client.patch(
                    f"/api/v1/members/{created_member_id}",
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": admin_session.csrf_token.reveal(),
                        "Idempotency-Key": "disable-new-member",
                    },
                    json={
                        "display_name": "New Member",
                        "status": "disabled",
                        "system_role": "member",
                    },
                )
                assert disabled_member.status_code == 200
                assert disabled_member.json()["status"] == "disabled"
                assert disabled_member.json()["mfa_enabled"] is False

                current_settings = await admin_client.get("/api/v1/settings")
                assert current_settings.status_code == 200
                assert current_settings.json()["revision"] == 0
                settings_draft = await admin_client.post(
                    "/api/v1/settings/drafts",
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": admin_session.csrf_token.reveal(),
                        "Idempotency-Key": "settings-draft-one",
                    },
                    json={
                        "base_revision": 0,
                        "reason": "Tune retrieval defaults",
                        "values": {"retrieval": {"limit": 15}},
                    },
                )
                assert settings_draft.status_code == 201
                settings_activation = await admin_client.post(
                    f"/api/v1/settings/drafts/{settings_draft.json()['id']}/activate",
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": admin_session.csrf_token.reveal(),
                        "Idempotency-Key": "settings-activate-one",
                    },
                    json={
                        "expected_active_revision": 0,
                        "reason": "Validated retrieval defaults",
                    },
                )
                assert settings_activation.status_code == 200
                assert settings_activation.json()["revision"] == 1
                assert settings_activation.json()["values"]["retrieval"]["limit"] == 15
                second_settings_draft = await admin_client.post(
                    "/api/v1/settings/drafts",
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": admin_session.csrf_token.reveal(),
                        "Idempotency-Key": "settings-draft-two",
                    },
                    json={
                        "base_revision": 1,
                        "reason": "Measure broader retrieval defaults",
                        "values": {"retrieval": {"limit": 25}},
                    },
                )
                assert second_settings_draft.status_code == 201
                second_settings_activation = await admin_client.post(
                    f"/api/v1/settings/drafts/{second_settings_draft.json()['id']}/activate",
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": admin_session.csrf_token.reveal(),
                        "Idempotency-Key": "settings-activate-two",
                    },
                    json={
                        "expected_active_revision": 1,
                        "reason": "Activate broader retrieval defaults",
                    },
                )
                assert second_settings_activation.status_code == 200
                settings_history = await admin_client.get(
                    "/api/v1/settings/history",
                    params={"page": 1, "page_size": 1},
                )
                assert settings_history.status_code == 200
                assert [item["revision"] for item in settings_history.json()["items"]] == [2]
                settings_history_next = await admin_client.get(
                    "/api/v1/settings/history",
                    params={"page": 2, "page_size": 1},
                )
                assert [item["revision"] for item in settings_history_next.json()["items"]] == [1]
                audit_page = await admin_client.get(
                    "/api/v1/audit-events",
                    params={"page": 1, "page_size": 1},
                )
                assert audit_page.status_code == 200
                audit_next = await admin_client.get(
                    "/api/v1/audit-events",
                    params={"page": 2, "page_size": 1},
                )
                assert audit_page.json()["items"][0]["id"] != audit_next.json()["items"][0]["id"]
                settings_rollback = await admin_client.post(
                    "/api/v1/settings/rollback/1",
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": admin_session.csrf_token.reveal(),
                        "Idempotency-Key": "settings-rollback-one",
                    },
                    json={
                        "expected_active_revision": 2,
                        "reason": "Restore validated retrieval defaults",
                    },
                )
                assert settings_rollback.status_code == 200
                assert settings_rollback.json()["revision"] == 3
                assert settings_rollback.json()["values"]["retrieval"]["limit"] == 15

        finally:
            set_session_factory(None)

        async with factory.begin() as session:
            created_member_id = await session.scalar(
                select(MemberModel.id).where(MemberModel.username_normalized == "new-member")
            )
            credential = await session.scalar(
                select(PasswordCredentialModel).where(
                    PasswordCredentialModel.member_id == created_member_id,
                    PasswordCredentialModel.retired_at.is_(None),
                )
            )
            assert credential is not None
            assert temporary_password not in credential.password_hash
            assert reset_password not in credential.password_hash
            assert OpaqueTokenCodec().digest(raw_key) != credential.password_hash
            session_revoke_audits = list(
                await session.scalars(
                    select(AuditEventModel).where(AuditEventModel.action == "session.revoked")
                )
            )
            assert len(session_revoke_audits) == 1


async def test_knowledge_listing_and_detail_are_scoped_to_effective_spaces(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    admin_id = uuid4()
    member_id = uuid4()
    settings = Settings(
        gateway={
            "environment": "test",
            "public_base_url": "https://gateway.test",
            "api_key_peppers": {1: "p" * 32},
            "active_api_key_pepper_version": 1,
            "mfa_encryption_keys": {1: Fernet.generate_key().decode("ascii")},
            "active_mfa_encryption_key_version": 1,
            "storage_dir": str(tmp_path / "storage"),
        }
    )

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(
                        id=admin_id,
                        username="admin",
                        username_normalized="admin",
                        display_name="Admin",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.SUPER_ADMIN.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=member_id,
                        username="limited-member",
                        username_normalized="limited-member",
                        display_name="Limited Member",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    Workspace(id="shared-space", name="Shared", created_by_member_id=admin_id),
                    Workspace(id="secret-space", name="Secret", created_by_member_id=admin_id),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    SpaceMembershipModel(
                        id=uuid4(),
                        space_id="shared-space",
                        member_id=member_id,
                        role=SpaceRole.EDITOR.value,
                    ),
                    SpaceMembershipModel(
                        id=uuid4(),
                        space_id="secret-space",
                        member_id=admin_id,
                        role=SpaceRole.OWNER.value,
                    ),
                    SpaceMembershipModel(
                        id=uuid4(),
                        space_id="shared-space",
                        member_id=admin_id,
                        role=SpaceRole.OWNER.value,
                    ),
                ]
            )
            admin_session = await SessionService(session).issue(
                principal(admin_id, SystemRole.SUPER_ADMIN),
                now=now,
                step_up_at=now,
            )
            member_session = await SessionService(session).issue(
                principal(member_id),
                now=now,
                step_up_at=now,
            )

        app = FastAPI()
        app.state.settings = settings
        register_exception_handlers(app)
        app.add_middleware(
            APIKeyAuthMiddleware,
            allowed_keys=[],
            api_key_peppers={1: "p" * 32},
            session_factory=factory,
        )
        app.add_middleware(SettingsContextMiddleware)
        app.include_router(router)
        set_session_factory(factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-aigw-access": admin_session.access_token.reveal()},
            ) as admin_client:
                admin_headers = {
                    "Origin": "https://gateway.test",
                    "X-CSRF-Token": admin_session.csrf_token.reveal(),
                }
                secret_item = await admin_client.post(
                    "/api/v1/knowledge",
                    headers={**admin_headers, "Idempotency-Key": "seed-secret-item"},
                    json={
                        "space_id": "secret-space",
                        "title": "Secret plan",
                        "content": "Confidential contents for the secret space.",
                        "tags": [],
                    },
                )
                assert secret_item.status_code == 201
                secret_item_id = secret_item.json()["id"]

                shared_item = await admin_client.post(
                    "/api/v1/knowledge",
                    headers={**admin_headers, "Idempotency-Key": "seed-shared-item"},
                    json={
                        "space_id": "shared-space",
                        "title": "Shared note",
                        "content": "Visible to space members.",
                        "tags": [],
                    },
                )
                assert shared_item.status_code == 201
                shared_item_id = shared_item.json()["id"]

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-aigw-access": member_session.access_token.reveal()},
            ) as limited_client:
                listing = await limited_client.get("/api/v1/knowledge")
                assert listing.status_code == 200
                listed_ids = [item["id"] for item in listing.json()["items"]]
                assert shared_item_id in listed_ids
                assert secret_item_id not in listed_ids

                scoped_to_secret = await limited_client.get("/api/v1/knowledge?space_id=secret-space")
                assert scoped_to_secret.status_code == 200
                assert scoped_to_secret.json()["items"] == []

                denied_detail = await limited_client.get(f"/api/v1/knowledge/{secret_item_id}")
                assert denied_detail.status_code in (403, 404)

                allowed_detail = await limited_client.get(f"/api/v1/knowledge/{shared_item_id}")
                assert allowed_detail.status_code == 200
        finally:
            set_session_factory(None)
