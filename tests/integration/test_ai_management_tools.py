from datetime import datetime, timezone
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from sqlalchemy import select

from src.gateway.application.services.session_service import SessionService
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SessionCredentialModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionModel, EmbeddingGenerationModel, IngestionJobModel
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.infrastructure.persistence.runtime_settings_models import RuntimeSettingRevisionModel
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.routers.management import router
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_destructive_ai_tool_requires_bound_one_time_website_confirmation() -> None:
    member_id = uuid4()
    second_member_id = uuid4()
    document_id = uuid4()
    document_revision_id = uuid4()
    queued_job_id = uuid4()
    failed_job_id = uuid4()
    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add_all(
                [MemberModel(
                    id=member_id,
                    username="member",
                    username_normalized="member",
                    display_name="Member",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.SUPER_ADMIN.value,
                    force_password_change=False,
                ), MemberModel(
                    id=second_member_id,
                    username="second",
                    username_normalized="second",
                    display_name="Second",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )]
            )
            session.add(Workspace(id="private", name="Private", created_by_member_id=member_id))
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    id=uuid4(),
                    space_id="private",
                    member_id=member_id,
                    role=SpaceRole.OWNER.value,
                )
            )
            await session.flush()
            session_time = datetime.now(timezone.utc)
            session.add(
                EmbeddingGenerationModel(
                    id=uuid4(),
                    purpose="retrieval",
                    model_id="offline-test",
                    dimensions=1024,
                    status="active",
                    activated_at=session_time,
                )
            )
            document = DocumentModel(
                id=document_id,
                space_id="private",
                display_name="Procedures",
                created_by_member_id=member_id,
            )
            session.add(document)
            await session.flush()
            revision = DocumentRevisionModel(
                id=document_revision_id,
                document_id=document_id,
                space_id="private",
                version=1,
                original_filename="procedures.txt",
                mime_type="text/plain",
                size_bytes=10,
                checksum_sha256="0" * 64,
                storage_key="spaces/private/documents/procedures/revisions/1/source",
                status="failed",
                created_by_member_id=member_id,
            )
            session.add(revision)
            await session.flush()
            document.current_revision_id = document_revision_id
            session.add_all(
                [
                    IngestionJobModel(
                        id=queued_job_id,
                        space_id="private",
                        document_id=document_id,
                        document_revision_id=document_revision_id,
                        initiated_by_member_id=member_id,
                        state="queued",
                        idempotency_key="queued-job",
                    ),
                    IngestionJobModel(
                        id=failed_job_id,
                        space_id="private",
                        document_id=document_id,
                        document_revision_id=document_revision_id,
                        initiated_by_member_id=member_id,
                        state="failed",
                        finished_at=session_time,
                        idempotency_key="failed-job",
                    ),
                ]
            )
            issued_session = await SessionService(session).issue(
                Principal(
                    subject_id=str(member_id),
                    kind=PrincipalKind.SESSION,
                    system_role=SystemRole.SUPER_ADMIN,
                    scopes=frozenset({"*"}),
                ),
                now=session_time,
                step_up_at=session_time,
            )
            session_credential_id = await session.scalar(
                select(SessionCredentialModel.id).where(
                    SessionCredentialModel.family_id == issued_session.family_id,
                    SessionCredentialModel.revoked_at.is_(None),
                )
            )
            assert session_credential_id is not None

        app = FastAPI()
        register_exception_handlers(app)

        @app.middleware("http")
        async def test_principal(request: Request, call_next):
            kind = PrincipalKind(request.headers.get("X-Test-Principal-Kind", "api_key"))
            scopes = frozenset(request.headers.get("X-Test-Scopes", "*").split(","))
            request.state.principal = Principal(
                subject_id=str(member_id),
                kind=kind,
                system_role=SystemRole.SUPER_ADMIN,
                scopes=scopes,
                credential_id=request.headers.get("X-Test-Credential-ID", str(uuid4())),
            )
            return await call_next(request)

        app.include_router(router)
        set_session_factory(factory)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://gateway.test",
            ) as client:
                catalog = await client.get("/api/v1/ai-tools")
                assert catalog.status_code == 200
                assert {item["name"] for item in catalog.json()["items"]} == {
                    "ingestion_jobs.cancel.v1",
                    "ingestion_jobs.list.v1",
                    "ingestion_jobs.retry.v1",
                    "knowledge.archive.v1",
                    "knowledge.create.v1",
                    "knowledge.read.v1",
                    "knowledge.search.v1",
                    "knowledge.update.v1",
                    "retrieval.explain.v1",
                    "settings.inspect.v1",
                    "settings.propose.v1",
                    "sources.list.v1",
                    "spaces.list.v1",
                    "spaces.create.v1",
                    "spaces.archive.v1",
                    "spaces.members.list.v1",
                    "spaces.members.set.v1",
                }
                read_catalog = await client.get(
                    "/api/v1/ai-tools",
                    headers={"X-Test-Scopes": "knowledge:read"},
                )
                assert {item["name"] for item in read_catalog.json()["items"]} == {
                    "ingestion_jobs.list.v1",
                    "knowledge.read.v1",
                    "knowledge.search.v1",
                    "retrieval.explain.v1",
                    "sources.list.v1",
                }
                listed = await client.post(
                    "/api/v1/ai-tools/spaces.list.v1",
                    headers={"Idempotency-Key": "list-spaces"},
                    json={"arguments": {"limit": 20}},
                )
                assert listed.status_code == 200
                assert [(item["id"], item["role"]) for item in listed.json()["items"]] == [
                    ("private", "owner")
                ]
                created = await client.post(
                    "/api/v1/ai-tools/spaces.create.v1",
                    headers={"Idempotency-Key": "create-space"},
                    json={"arguments": {"name": "Travel"}},
                )
                assert created.status_code == 201
                assert created.json()["result"]["name"] == "Travel"

                memberships = await client.post(
                    "/api/v1/ai-tools/spaces.members.list.v1",
                    headers={"Idempotency-Key": "list-private-members"},
                    json={"arguments": {"space_id": "private"}},
                )
                assert memberships.status_code == 200
                assert memberships.json()["result"]["items"][0]["member_id"] == str(member_id)

                knowledge = await client.post(
                    "/api/v1/ai-tools/knowledge.create.v1",
                    headers={"Idempotency-Key": "create-knowledge"},
                    json={"arguments": {"space_id": "private", "title": "Valve", "content": "Turn the valve clockwise.", "tags": ["home"]}},
                )
                assert knowledge.status_code == 201
                knowledge_id = knowledge.json()["result"]["id"]
                read_knowledge = await client.post(
                    "/api/v1/ai-tools/knowledge.read.v1",
                    headers={"Idempotency-Key": "read-knowledge"},
                    json={"arguments": {"item_id": knowledge_id}},
                )
                assert read_knowledge.status_code == 200
                assert read_knowledge.json()["result"]["content"] == "Turn the valve clockwise."
                invalid_knowledge = await client.post(
                    "/api/v1/ai-tools/knowledge.create.v1",
                    headers={"Idempotency-Key": "invalid-knowledge-tag"},
                    json={"arguments": {"space_id": "private", "title": "Invalid", "content": "Invalid tag.", "tags": ["x" * 101]}},
                )
                assert invalid_knowledge.status_code == 422
                updated_knowledge = await client.post(
                    "/api/v1/ai-tools/knowledge.update.v1",
                    headers={"Idempotency-Key": "update-knowledge"},
                    json={"arguments": {"item_id": knowledge_id, "expected_version": 1, "title": "Valve", "content": "Turn the valve clockwise before repairs.", "tags": ["home"], "change_summary": "Clarify timing"}},
                )
                assert updated_knowledge.status_code == 200
                assert updated_knowledge.json()["result"]["version"] == 2

                searched_knowledge = await client.post(
                    "/api/v1/ai-tools/knowledge.search.v1",
                    headers={"Idempotency-Key": "search-knowledge"},
                    json={"arguments": {"query": "repairs", "space_ids": ["private"], "semantic_policy": "disabled", "limit": 10}},
                )
                assert searched_knowledge.status_code == 200
                assert searched_knowledge.json()["result"]["hits"][0]["canonical_id"] == knowledge_id
                explained_retrieval = await client.post(
                    "/api/v1/ai-tools/retrieval.explain.v1",
                    headers={"Idempotency-Key": "explain-retrieval"},
                    json={"arguments": {"query": "repairs", "space_ids": ["private"], "semantic_policy": "disabled", "limit": 10}},
                )
                assert explained_retrieval.status_code == 200
                assert explained_retrieval.json()["result"]["explanation"]["effective_space_ids"] == ["private"]

                sources = await client.post(
                    "/api/v1/ai-tools/sources.list.v1",
                    headers={"Idempotency-Key": "list-sources"},
                    json={"arguments": {"space_id": "private", "limit": 20}},
                )
                assert sources.status_code == 200
                assert [item["id"] for item in sources.json()["result"]["items"]] == [str(document_id)]
                jobs = await client.post(
                    "/api/v1/ai-tools/ingestion_jobs.list.v1",
                    headers={"Idempotency-Key": "list-jobs"},
                    json={"arguments": {"space_id": "private", "limit": 20}},
                )
                assert jobs.status_code == 200
                assert {item["id"] for item in jobs.json()["result"]["items"]} == {str(queued_job_id), str(failed_job_id)}
                inspected_settings = await client.post(
                    "/api/v1/ai-tools/settings.inspect.v1",
                    headers={"Idempotency-Key": "inspect-settings"},
                    json={"arguments": {}},
                )
                assert inspected_settings.status_code == 200
                assert inspected_settings.json()["result"]["revision"] == 0

                membership_proposal = await client.post(
                    "/api/v1/ai-tools/spaces.members.set.v1",
                    headers={"Idempotency-Key": "propose-second-reader"},
                    json={"arguments": {"space_id": "private", "member_id": str(second_member_id), "role": "reader", "expected_space_revision": 1}},
                )
                assert membership_proposal.status_code == 202
                membership_pending_id = membership_proposal.json()["pending_action_id"]
                confirmed_membership = await client.post(
                    f"/api/v1/ai-actions/{membership_pending_id}/confirm",
                    headers={"Idempotency-Key": "confirm-second-reader", "X-Test-Principal-Kind": "session"},
                )
                assert confirmed_membership.status_code == 200

                knowledge_archive = await client.post(
                    "/api/v1/ai-tools/knowledge.archive.v1",
                    headers={"Idempotency-Key": "archive-knowledge"},
                    json={"arguments": {"item_id": knowledge_id, "expected_version": 2}},
                )
                assert knowledge_archive.status_code == 202
                knowledge_pending_id = knowledge_archive.json()["pending_action_id"]
                confirmed_knowledge = await client.post(
                    f"/api/v1/ai-actions/{knowledge_pending_id}/confirm",
                    headers={"Idempotency-Key": "confirm-knowledge-archive", "X-Test-Principal-Kind": "session"},
                )
                assert confirmed_knowledge.status_code == 200

                settings_proposal = await client.post(
                    "/api/v1/ai-tools/settings.propose.v1",
                    headers={"Idempotency-Key": "propose-settings"},
                    json={"arguments": {"base_revision": 0, "values": {"retrieval": {"limit": 13}}, "reason": "Tune focused family retrieval"}},
                )
                assert settings_proposal.status_code == 202
                settings_pending_id = settings_proposal.json()["pending_action_id"]
                confirmed_settings = await client.post(
                    f"/api/v1/ai-actions/{settings_pending_id}/confirm",
                    headers={
                        "Idempotency-Key": "confirm-settings-proposal",
                        "X-Test-Principal-Kind": "session",
                        "X-Test-Credential-ID": str(session_credential_id),
                    },
                )
                assert confirmed_settings.status_code == 200

                cancel_proposal = await client.post(
                    "/api/v1/ai-tools/ingestion_jobs.cancel.v1",
                    headers={"Idempotency-Key": "propose-job-cancel"},
                    json={"arguments": {"job_id": str(queued_job_id), "expected_state": "queued"}},
                )
                assert cancel_proposal.status_code == 202
                confirmed_cancel = await client.post(
                    f"/api/v1/ai-actions/{cancel_proposal.json()['pending_action_id']}/confirm",
                    headers={"Idempotency-Key": "confirm-job-cancel", "X-Test-Principal-Kind": "session"},
                )
                assert confirmed_cancel.status_code == 200

                retry_proposal = await client.post(
                    "/api/v1/ai-tools/ingestion_jobs.retry.v1",
                    headers={"Idempotency-Key": "propose-job-retry"},
                    json={"arguments": {"job_id": str(failed_job_id), "expected_state": "failed"}},
                )
                assert retry_proposal.status_code == 202
                confirmed_retry = await client.post(
                    f"/api/v1/ai-actions/{retry_proposal.json()['pending_action_id']}/confirm",
                    headers={"Idempotency-Key": "confirm-job-retry", "X-Test-Principal-Kind": "session"},
                )
                assert confirmed_retry.status_code == 200

                proposed = await client.post(
                    "/api/v1/ai-tools/spaces.archive.v1",
                    headers={"Idempotency-Key": "archive-private"},
                    json={"arguments": {"space_id": "private", "expected_revision": 2}},
                )
                assert proposed.status_code == 202
                assert proposed.json()["status"] == "confirmation_required"
                pending_id = proposed.json()["pending_action_id"]

                model_cannot_confirm = await client.post(
                    f"/api/v1/ai-actions/{pending_id}/confirm",
                    headers={
                        "Idempotency-Key": "confirm-private-api-key",
                        "X-Test-Principal-Kind": "api_key",
                    },
                )
                assert model_cannot_confirm.status_code == 404
                assert model_cannot_confirm.json()["error"]["type"] == "authorization_error"

                confirmed = await client.post(
                    f"/api/v1/ai-actions/{pending_id}/confirm",
                    headers={
                        "Idempotency-Key": "confirm-private-session",
                        "X-Test-Principal-Kind": "session",
                    },
                )
                assert confirmed.status_code == 200
                assert confirmed.json() == {
                    "pending_action_id": pending_id,
                    "status": "executed",
                    "tool_name": "spaces.archive.v1",
                }

                replay = await client.post(
                    f"/api/v1/ai-actions/{pending_id}/confirm",
                    headers={
                        "Idempotency-Key": "confirm-private-again",
                        "X-Test-Principal-Kind": "session",
                    },
                )
                assert replay.status_code == 409
        finally:
            set_session_factory(None)

        async with factory.begin() as session:
            space = await session.get(Workspace, "private")
            assert space is not None and space.archived_at is not None and space.revision == 3
            settings_draft = await session.scalar(
                select(RuntimeSettingRevisionModel).where(RuntimeSettingRevisionModel.state == "draft")
            )
            assert settings_draft is not None
            assert settings_draft.values["retrieval"]["limit"] == 13
            queued_job = await session.get(IngestionJobModel, queued_job_id)
            failed_job = await session.get(IngestionJobModel, failed_job_id)
            assert queued_job is not None and queued_job.cancellation_requested
            assert failed_job is not None and failed_job.retry_requested
