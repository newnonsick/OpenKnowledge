from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from src.gateway.application.services.session_service import SessionService
from src.gateway.application.services.runtime_settings_service import RuntimeSettingsValues
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import (
    AuditEventModel,
    MemberModel,
    PendingAIActionModel,
    PersonalAPIKeyModel,
    SessionFamilyModel,
    SpaceMembershipModel,
)
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionModel, IngestionJobModel
from src.gateway.infrastructure.persistence.models import KnowledgeItem, Workspace
from src.gateway.infrastructure.persistence.runtime_settings_models import RuntimeSettingRevisionModel
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.routers.management import router
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


def session_principal(member_id):
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=SystemRole.SUPER_ADMIN,
        scopes=frozenset({"*"}),
    )


@asynccontextmanager
async def pagination_client(tmp_path):
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    alpha_id = uuid4()
    beta_id = uuid4()
    gamma_id = uuid4()
    disabled_id = uuid4()
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
            handbook_id = uuid4()
            handbook_revision_id = uuid4()
            incident_id = uuid4()
            incident_revision_id = uuid4()
            session.add_all(
                [
                    MemberModel(
                        id=owner_id,
                        username="owner",
                        username_normalized="owner",
                        display_name="Owner",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.SUPER_ADMIN.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=alpha_id,
                        username="alpha",
                        username_normalized="alpha",
                        display_name="Alpha Reader",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=beta_id,
                        username="beta",
                        username_normalized="beta",
                        display_name="Beta Editor",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=gamma_id,
                        username="gamma",
                        username_normalized="gamma",
                        display_name="Gamma Candidate",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=disabled_id,
                        username="disabled",
                        username_normalized="disabled",
                        display_name="Disabled Member",
                        status=MemberStatus.DISABLED.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    Workspace(id="private", name="Private", created_by_member_id=owner_id),
                    Workspace(id="zeta", name="Zeta Operations", created_by_member_id=owner_id),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    KnowledgeItem(
                        id=UUID(int=1),
                        workspace_id="private",
                        title="Quarterly Finance Plan",
                        content="Revenue targets and budget allocation",
                        tags=["finance", "planning"],
                        created_at=now - timedelta(days=2),
                        updated_at=now - timedelta(days=2),
                    ),
                    KnowledgeItem(
                        id=UUID(int=2),
                        workspace_id="private",
                        title="Engineering Runbook",
                        content="Service recovery procedures",
                        tags=["engineering"],
                        created_at=now - timedelta(days=1),
                        updated_at=now - timedelta(days=1),
                    ),
                    DocumentModel(
                        id=handbook_id,
                        space_id="private",
                        display_name="Employee Handbook",
                        created_by_member_id=owner_id,
                    ),
                    DocumentModel(
                        id=incident_id,
                        space_id="private",
                        display_name="Incident Report",
                        created_by_member_id=owner_id,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    DocumentRevisionModel(
                        id=handbook_revision_id,
                        document_id=handbook_id,
                        space_id="private",
                        version=1,
                        original_filename="handbook.pdf",
                        mime_type="application/pdf",
                        size_bytes=1024,
                        checksum_sha256="a" * 64,
                        status="ready",
                        created_by_member_id=owner_id,
                    ),
                    DocumentRevisionModel(
                        id=incident_revision_id,
                        document_id=incident_id,
                        space_id="private",
                        version=1,
                        original_filename="incident.txt",
                        mime_type="text/plain",
                        size_bytes=512,
                        checksum_sha256="b" * 64,
                        status="failed",
                        created_by_member_id=owner_id,
                    ),
                    SpaceMembershipModel(id=uuid4(), space_id="private", member_id=owner_id, role=SpaceRole.OWNER.value),
                    SpaceMembershipModel(id=uuid4(), space_id="private", member_id=alpha_id, role=SpaceRole.READER.value),
                    SpaceMembershipModel(id=uuid4(), space_id="private", member_id=beta_id, role=SpaceRole.EDITOR.value),
                    SpaceMembershipModel(id=uuid4(), space_id="zeta", member_id=owner_id, role=SpaceRole.OWNER.value),
                    PendingAIActionModel(
                        id=uuid4(),
                        actor_member_id=owner_id,
                        proposed_by_kind="session",
                        tool_name="knowledge.archive.v1",
                        normalized_command={"target": "one"},
                        command_hash="1" * 64,
                        target_ids=["one"],
                        expected_revision=1,
                        state="pending",
                        expires_at=now + timedelta(hours=1),
                        created_at=now - timedelta(minutes=2),
                    ),
                    PendingAIActionModel(
                        id=uuid4(),
                        actor_member_id=owner_id,
                        proposed_by_kind="session",
                        tool_name="knowledge.archive.v1",
                        normalized_command={"target": "two"},
                        command_hash="2" * 64,
                        target_ids=["two"],
                        expected_revision=1,
                        state="pending",
                        expires_at=now + timedelta(hours=1),
                        created_at=now - timedelta(minutes=1),
                    ),
                    PendingAIActionModel(
                        id=uuid4(),
                        actor_member_id=owner_id,
                        proposed_by_kind="session",
                        tool_name="knowledge.archive.v1",
                        normalized_command={"target": "three"},
                        command_hash="3" * 64,
                        target_ids=["three"],
                        expected_revision=1,
                        state="pending",
                        expires_at=now + timedelta(hours=1),
                        created_at=now,
                    ),
                ]
            )
            await session.flush()
            handbook = await session.get(DocumentModel, handbook_id)
            incident = await session.get(DocumentModel, incident_id)
            handbook.current_revision_id = handbook_revision_id
            incident.current_revision_id = incident_revision_id
            session.add_all(
                [
                    IngestionJobModel(
                        id=uuid4(),
                        space_id="private",
                        document_id=handbook_id,
                        document_revision_id=handbook_revision_id,
                        initiated_by_member_id=owner_id,
                        state="queued",
                        idempotency_key="handbook-job",
                    ),
                    IngestionJobModel(
                        id=uuid4(),
                        space_id="private",
                        document_id=incident_id,
                        document_revision_id=incident_revision_id,
                        initiated_by_member_id=owner_id,
                        state="failed",
                        idempotency_key="incident-job",
                        finished_at=now,
                    ),
                    PersonalAPIKeyModel(
                        id=uuid4(),
                        member_id=owner_id,
                        public_id="kb_active_filter",
                        key_digest="c" * 64,
                        name="Production Search",
                        status="active",
                    ),
                    PersonalAPIKeyModel(
                        id=uuid4(),
                        member_id=owner_id,
                        public_id="kb_revoked_filter",
                        key_digest="d" * 64,
                        name="Retired Automation",
                        status="revoked",
                        revoked_at=now,
                    ),
                    SessionFamilyModel(
                        id=uuid4(),
                        member_id=owner_id,
                        created_at=now - timedelta(days=2),
                        last_activity_at=now - timedelta(days=1),
                        idle_expires_at=now - timedelta(hours=1),
                        absolute_expires_at=now - timedelta(minutes=1),
                        csrf_token_digest="e" * 64,
                    ),
                    SessionFamilyModel(
                        id=uuid4(),
                        member_id=owner_id,
                        created_at=now - timedelta(days=3),
                        last_activity_at=now - timedelta(days=2),
                        idle_expires_at=now + timedelta(days=1),
                        absolute_expires_at=now + timedelta(days=2),
                        revoked_at=now - timedelta(days=1),
                        revoke_reason="test",
                    ),
                    AuditEventModel(
                        id=uuid4(),
                        occurred_at=now,
                        actor_member_id=owner_id,
                        actor_kind="session",
                        request_id="request-settings-activation",
                        action="settings.activated",
                        resource_type="runtime_settings",
                        resource_id="2",
                        outcome="success",
                    ),
                    AuditEventModel(
                        id=uuid4(),
                        occurred_at=now - timedelta(minutes=1),
                        actor_member_id=owner_id,
                        actor_kind="session",
                        request_id="request-member-denied",
                        action="member.updated",
                        resource_type="member",
                        resource_id=str(disabled_id),
                        outcome="denied",
                    ),
                    RuntimeSettingRevisionModel(
                        id=uuid4(),
                        revision=1,
                        base_revision=0,
                        state="superseded",
                        values=RuntimeSettingsValues().model_dump(mode="json"),
                        draft_reason="Initial configuration",
                        activation_reason="Initial activation",
                        created_by_member_id=owner_id,
                        activated_by_member_id=owner_id,
                        activated_at=now - timedelta(days=1),
                    ),
                    RuntimeSettingRevisionModel(
                        id=uuid4(),
                        revision=2,
                        base_revision=1,
                        state="active",
                        values=RuntimeSettingsValues().model_dump(mode="json"),
                        draft_reason="Current configuration",
                        activation_reason="Current activation",
                        created_by_member_id=owner_id,
                        activated_by_member_id=owner_id,
                        activated_at=now,
                    ),
                ]
            )
            issued = await SessionService(session).issue(session_principal(owner_id), now=now, step_up_at=now)

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
                cookies={"__Host-openknowledge-access": issued.access_token.reveal()},
                headers={
                    "Origin": "https://gateway.test",
                    "X-CSRF-Token": issued.csrf_token.reveal(),
                },
            ) as client:
                yield client
        finally:
            set_session_factory(None)


async def test_space_members_support_numeric_pages_search_and_role_filters(tmp_path) -> None:
    async with pagination_client(tmp_path) as client:
        first = await client.get("/api/v1/spaces/private/members", params={"page": 1, "page_size": 2})

        assert first.status_code == 200
        assert [item["username"] for item in first.json()["items"]] == ["alpha", "beta"]
        assert first.json()["total_items"] == 3
        assert first.json()["total_pages"] == 2

        second = await client.get(
            "/api/v1/spaces/private/members",
            params={"page": 2, "page_size": 2},
        )

        assert second.status_code == 200
        assert [item["username"] for item in second.json()["items"]] == ["owner"]
        assert second.json()["page"] == 2

        searched = await client.get("/api/v1/spaces/private/members", params={"q": "BETA"})
        readers = await client.get("/api/v1/spaces/private/members", params={"role": "reader"})

        assert [item["username"] for item in searched.json()["items"]] == ["beta"]
        assert [item["username"] for item in readers.json()["items"]] == ["alpha"]


async def test_pending_ai_actions_support_stable_numeric_pages(tmp_path) -> None:
    async with pagination_client(tmp_path) as client:
        first = await client.get("/api/v1/ai-actions", params={"page": 1, "page_size": 2})

        assert first.status_code == 200
        assert [item["target_ids"] for item in first.json()["items"]] == [["three"], ["two"]]
        assert first.json()["total_items"] == 3
        assert first.json()["total_pages"] == 2

        second = await client.get(
            "/api/v1/ai-actions",
            params={"page": 2, "page_size": 2},
        )

        assert second.status_code == 200
        assert [item["target_ids"] for item in second.json()["items"]] == [["one"]]
        assert second.json()["page"] == 2


async def test_spaces_candidates_and_members_support_server_side_filters(tmp_path) -> None:
    async with pagination_client(tmp_path) as client:
        spaces = await client.get("/api/v1/spaces", params={"q": "operations"})
        admin_spaces = await client.get("/api/v1/admin/spaces", params={"q": "ZETA"})
        candidates = await client.get(
            "/api/v1/spaces/private/member-candidates",
            params={"q": "candidate"},
        )
        members = await client.get(
            "/api/v1/members",
            params={"q": "owner", "status": "active", "system_role": "super_admin"},
        )

        assert spaces.status_code == 200
        assert [item["id"] for item in spaces.json()["items"]] == ["zeta"]
        assert [item["id"] for item in admin_spaces.json()["items"]] == ["zeta"]
        assert [item["username"] for item in candidates.json()["items"]] == ["gamma"]
        assert [item["username"] for item in members.json()["items"]] == ["owner"]


async def test_knowledge_sources_and_jobs_support_server_side_filters(tmp_path) -> None:
    async with pagination_client(tmp_path) as client:
        knowledge = await client.get(
            "/api/v1/knowledge",
            params={"q": "quarterly", "tag": "finance"},
        )
        sources = await client.get(
            "/api/v1/sources",
            params={"q": "handbook", "status": "ready"},
        )
        jobs = await client.get("/api/v1/ingestion-jobs", params={"state": "failed"})

        assert knowledge.status_code == 200
        assert [item["title"] for item in knowledge.json()["items"]] == ["Quarterly Finance Plan"]
        assert [item["display_name"] for item in sources.json()["items"]] == ["Employee Handbook"]
        assert [item["state"] for item in jobs.json()["items"]] == ["failed"]


async def test_knowledge_pages_show_recent_changes_first_with_numeric_pages(tmp_path) -> None:
    async with pagination_client(tmp_path) as client:
        created = await client.post(
            "/api/v1/knowledge",
            headers={"Idempotency-Key": "create-recent-knowledge"},
            json={
                "space_id": "private",
                "title": "Most recent family note",
                "content": "This item should be visible immediately after creation.",
                "tags": ["recent"],
            },
        )

        assert created.status_code == 201
        first = await client.get("/api/v1/knowledge", params={"page": 1, "page_size": 1})
        assert first.status_code == 200
        assert [item["id"] for item in first.json()["items"]] == [created.json()["id"]]
        assert first.json()["total_items"] == 3
        assert first.json()["total_pages"] == 3

        second = await client.get(
            "/api/v1/knowledge",
            params={"page": 2, "page_size": 1},
        )
        assert second.status_code == 200
        assert second.json()["items"][0]["id"] != created.json()["id"]
        assert second.json()["page"] == 2


async def test_security_audit_and_settings_lists_support_server_side_filters(tmp_path) -> None:
    async with pagination_client(tmp_path) as client:
        keys = await client.get(
            "/api/v1/api-keys",
            params={"q": "production", "status": "active"},
        )
        sessions = await client.get("/api/v1/sessions", params={"status": "expired"})
        audits = await client.get(
            "/api/v1/audit-events",
            params={
                "q": "activation",
                "action": "settings.activated",
                "outcome": "success",
                "resource_type": "runtime_settings",
            },
        )
        settings = await client.get("/api/v1/settings/history", params={"state": "active"})

        assert [item["name"] for item in keys.json()["items"]] == ["Production Search"]
        assert [item["status"] for item in sessions.json()["items"]] == ["expired"]
        assert [item["action"] for item in audits.json()["items"]] == ["settings.activated"]
        assert [item["revision"] for item in settings.json()["items"]] == [2]


async def test_management_lists_expose_numeric_pages_totals_and_direct_jumps(tmp_path) -> None:
    async with pagination_client(tmp_path) as client:
        first = await client.get(
            "/api/v1/spaces/private/members",
            params={"page": 1, "page_size": 2},
        )
        second = await client.get(
            "/api/v1/spaces/private/members",
            params={"page": 2, "page_size": 2},
        )
        beyond = await client.get(
            "/api/v1/spaces/private/members",
            params={"page": 9, "page_size": 2},
        )
        empty = await client.get(
            "/api/v1/spaces/private/members",
            params={"page": 9, "page_size": 2, "q": "no matching member"},
        )

        assert first.status_code == 200
        assert first.json()["page"] == 1
        assert first.json()["page_size"] == 2
        assert first.json()["total_items"] == 3
        assert first.json()["total_pages"] == 2
        assert [item["username"] for item in first.json()["items"]] == ["alpha", "beta"]
        assert [item["username"] for item in second.json()["items"]] == ["owner"]
        assert second.json()["page"] == 2
        assert beyond.status_code == 200
        assert beyond.json()["page"] == 2
        assert beyond.json()["total_items"] == 3
        assert beyond.json()["total_pages"] == 2
        assert [item["username"] for item in beyond.json()["items"]] == ["owner"]
        assert empty.status_code == 200
        assert empty.json()["page"] == 1
        assert empty.json()["total_items"] == 0
        assert empty.json()["total_pages"] == 0
        assert empty.json()["items"] == []


async def test_management_pages_validate_parameters_and_keep_filter_totals(tmp_path) -> None:
    async with pagination_client(tmp_path) as client:
        invalid_page = await client.get("/api/v1/spaces", params={"page": 0})
        invalid_size = await client.get("/api/v1/spaces", params={"page_size": 101})
        filtered = await client.get(
            "/api/v1/knowledge",
            params={"page": 1, "page_size": 1, "q": "quarterly", "tag": "finance"},
        )

        assert invalid_page.status_code == 422
        assert invalid_size.status_code == 422
        assert filtered.status_code == 200
        assert filtered.json()["total_items"] == 1
        assert filtered.json()["total_pages"] == 1
