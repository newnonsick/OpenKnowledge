from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

from src.gateway.application.services import knowledge_management_service as knowledge_management_module
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence import retrieval_unit_repository as retrieval_unit_repository_module
from src.gateway.infrastructure.persistence.identity_models import (
    AuditEventModel,
    MemberModel,
    SpaceMembershipModel,
)
from src.gateway.infrastructure.persistence.ingestion_models import (
    EmbeddingGenerationModel,
    MigrationBackfillRunModel,
)
from src.gateway.domain.entities import KnowledgeRevision as DomainKnowledgeRevision
from src.gateway.infrastructure.persistence.models import EMBED_DIM, KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.routers import management as management_module
from src.gateway.presentation.routers.management import router
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


def session_principal(member_id, system_role=SystemRole.MEMBER):
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=system_role,
        scopes=frozenset({"*"}),
    )


class StubEmbeddingClient:
    async def embed_query(self, query):
        return [0.1] * EMBED_DIM

    async def embed_texts(self, texts):
        return [[0.1] * EMBED_DIM for _ in texts]


def _revision(item_id, space_id, version, title="Stale title", content="Stale content"):
    return KnowledgeRevision(
        id=uuid4(),
        item_id=item_id,
        space_id=space_id,
        version=version,
        title=title,
        content_hash=DomainKnowledgeRevision.compute_hash(content),
        content=content,
        tags=[],
        author="seed",
    )


async def _seed_members_and_spaces(session, *, now, owner_id, reader_id):
    session.add_all(
        [
            MemberModel(
                id=owner_id,
                username="stale-owner",
                username_normalized="stale-owner",
                display_name="Stale Owner",
                status=MemberStatus.ACTIVE.value,
                system_role=SystemRole.SUPER_ADMIN.value,
                force_password_change=False,
            ),
            MemberModel(
                id=reader_id,
                username="stale-reader",
                username_normalized="stale-reader",
                display_name="Stale Reader",
                status=MemberStatus.ACTIVE.value,
                system_role=SystemRole.MEMBER.value,
                force_password_change=False,
            ),
            Workspace(id="ops-space", name="Ops", created_by_member_id=owner_id),
            Workspace(id="secret-space", name="Secret", created_by_member_id=owner_id),
        ]
    )
    await session.flush()
    session.add_all(
        [
            SpaceMembershipModel(id=uuid4(), space_id="ops-space", member_id=owner_id, role=SpaceRole.OWNER.value),
            SpaceMembershipModel(id=uuid4(), space_id="secret-space", member_id=owner_id, role=SpaceRole.OWNER.value),
            SpaceMembershipModel(id=uuid4(), space_id="ops-space", member_id=reader_id, role=SpaceRole.READER.value),
        ]
    )
    owner_session = await SessionService(session).issue(
        session_principal(owner_id, SystemRole.SUPER_ADMIN), now=now, step_up_at=now
    )
    reader_session = await SessionService(session).issue(session_principal(reader_id), now=now, step_up_at=now)
    return owner_session, reader_session


def _seed_knowledge(session, *, now):
    return {
        "old": (now - timedelta(days=120), 2, "Old runbook", "Old content"),
        "older": (now - timedelta(days=200), 1, "Older runbook", "Older content"),
        "fresh": (now - timedelta(days=5), 1, "Fresh runbook", "Fresh content"),
        "secret": (now - timedelta(days=300), 3, "Secret runbook", "Secret content"),
    }


async def _persist_knowledge(session, *, now, space_by_key):
    specs = _seed_knowledge(session, now=now)
    ids = {}
    revisions = []
    for key, (stamped_at, version, title, content) in specs.items():
        item_id = uuid4()
        ids[key] = item_id
        session.add(
            KnowledgeItem(
                id=item_id,
                workspace_id=space_by_key[key],
                title=title,
                content=content,
                tags=[],
                revision=version,
                created_at=stamped_at,
                updated_at=stamped_at,
            )
        )
        revisions.append(_revision(item_id, space_by_key[key], version, title=title, content=content))
    session.add_all(revisions)
    await session.flush()
    items = {item.id: item for item in await session.scalars(select(KnowledgeItem))}
    by_item = {revision.item_id: revision for revision in revisions}
    for item_id, revision in by_item.items():
        items[item_id].current_revision_id = revision.id
    await session.flush()
    for key, (stamped_at, _, _, _) in specs.items():
        items[ids[key]].created_at = stamped_at
        items[ids[key]].updated_at = stamped_at
    await session.flush()
    return ids["old"], ids["older"], ids["fresh"], ids["secret"], revisions


def _test_settings(tmp_path):
    return Settings(
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


def _build_app(settings, factory):
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
    return app


def _auth_headers(session):
    return {
        "Origin": "https://gateway.test",
        "X-CSRF-Token": session.csrf_token.reveal(),
    }


async def test_stale_list_ordering_threshold_space_scope_and_summary(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(management_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _test_settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, reader_session = await _seed_members_and_spaces(
                session, now=now, owner_id=owner_id, reader_id=reader_id
            )
            old_id, older_id, fresh_id, secret_id, revisions = await _persist_knowledge(
                session, now=now, space_by_key={"old": "ops-space", "older": "ops-space", "fresh": "ops-space", "secret": "secret-space"}
            )

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                stale = await owner_client.get("/api/v1/knowledge/stale")
                assert stale.status_code == 200
                payload = stale.json()
                assert [item["id"] for item in payload["items"]] == [str(secret_id), str(older_id), str(old_id)]
                assert [item["version"] for item in payload["items"]] == [3, 1, 2]
                assert [item["space_id"] for item in payload["items"]] == ["secret-space", "ops-space", "ops-space"]
                assert [item["title"] for item in payload["items"]] == ["Secret runbook", "Older runbook", "Old runbook"]
                assert [item["age_days"] for item in payload["items"]] == [300, 200, 120]
                for item in payload["items"]:
                    assert datetime.fromisoformat(item["updated_at"]).tzinfo is not None
                assert str(fresh_id) not in [item["id"] for item in payload["items"]]

                scoped = await owner_client.get("/api/v1/knowledge/stale", params={"space_id": "ops-space"})
                assert scoped.status_code == 200
                assert [item["id"] for item in scoped.json()["items"]] == [str(older_id), str(old_id)]

                overridden = await owner_client.get(
                    "/api/v1/knowledge/stale", params={"space_id": "ops-space", "older_than_days": 150}
                )
                assert overridden.status_code == 200
                assert [item["id"] for item in overridden.json()["items"]] == [str(older_id)]

                first_page = await owner_client.get(
                    "/api/v1/knowledge/stale", params={"space_id": "ops-space", "page": 1, "page_size": 1}
                )
                second_page = await owner_client.get(
                    "/api/v1/knowledge/stale", params={"space_id": "ops-space", "page": 2, "page_size": 1}
                )
                assert first_page.json()["items"][0]["id"] == str(older_id)
                assert second_page.json()["items"][0]["id"] == str(old_id)
                assert first_page.json()["total_items"] == 2

                summary = await owner_client.get("/api/v1/operations/summary")
                assert summary.status_code == 200
                assert summary.json()["stale_knowledge"] == {"stale_after_days": 90, "stale_items": 3}

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": reader_session.access_token.reveal()},
            ) as reader_client:
                reader_stale = await reader_client.get("/api/v1/knowledge/stale")
                assert reader_stale.status_code == 200
                assert [item["id"] for item in reader_stale.json()["items"]] == [str(older_id), str(old_id)]

                denied_space = await reader_client.get("/api/v1/knowledge/stale", params={"space_id": "secret-space"})
                assert denied_space.status_code == 200
                assert denied_space.json()["items"] == []

                reader_summary = await reader_client.get("/api/v1/operations/summary")
                assert reader_summary.status_code == 200
                assert reader_summary.json()["stale_knowledge"] == {"stale_after_days": 90, "stale_items": 2}

            async with factory() as session:
                before = {
                    (item.id, item.updated_at, item.revision) for item in await session.scalars(select(KnowledgeItem))
                }
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                await owner_client.get("/api/v1/knowledge/stale", params={"space_id": "ops-space"})
            async with factory() as session:
                after = {
                    (item.id, item.updated_at, item.revision) for item in await session.scalars(select(KnowledgeItem))
                }
            assert before == after
        finally:
            set_session_factory(None)


async def test_reviewed_writes_audit_only_and_replays_idempotently(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(management_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _test_settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, reader_session = await _seed_members_and_spaces(
                session, now=now, owner_id=owner_id, reader_id=reader_id
            )
            old_id, _, _, _, revisions = await _persist_knowledge(
                session, now=now, space_by_key={"old": "ops-space", "older": "ops-space", "fresh": "ops-space", "secret": "secret-space"}
            )
            item = await session.get(KnowledgeItem, old_id)
            before_snapshot = (item.updated_at, item.revision, item.title)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                first = await owner_client.post(
                    f"/api/v1/knowledge/{old_id}/reviewed",
                    headers={**owner_headers, "Idempotency-Key": "review-old-runbook"},
                )
                assert first.status_code == 200
                assert first.json() == {"id": str(old_id), "status": "reviewed"}

                replay = await owner_client.post(
                    f"/api/v1/knowledge/{old_id}/reviewed",
                    headers={**owner_headers, "Idempotency-Key": "review-old-runbook"},
                )
                assert replay.status_code == 200
                assert replay.json() == {"id": str(old_id), "status": "reviewed"}

                missing = await owner_client.post(
                    f"/api/v1/knowledge/{uuid4()}/reviewed",
                    headers={**owner_headers, "Idempotency-Key": "review-missing"},
                )
                assert missing.status_code == 404

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": reader_session.access_token.reveal()},
            ) as reader_client:
                reader_headers = _auth_headers(reader_session)
                denied = await reader_client.post(
                    f"/api/v1/knowledge/{old_id}/reviewed",
                    headers={**reader_headers, "Idempotency-Key": "review-denied"},
                )
                assert denied.status_code == 404

            async with factory() as session:
                events = list(
                    await session.scalars(
                        select(AuditEventModel).where(
                            AuditEventModel.action == "knowledge.reviewed",
                            AuditEventModel.resource_id == str(old_id),
                        )
                    )
                )
                assert len(events) == 1
                assert events[0].resource_type == "knowledge_item"
                item = await session.get(KnowledgeItem, old_id)
                assert (item.updated_at, item.revision, item.title) == before_snapshot
        finally:
            set_session_factory(None)


async def test_reindex_status_shape_enqueue_idempotency_and_audit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(management_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _test_settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, reader_session = await _seed_members_and_spaces(
                session, now=now, owner_id=owner_id, reader_id=reader_id
            )
            active = EmbeddingGenerationModel(
                purpose="retrieval",
                model_id="embed-active",
                dimensions=1024,
                status="active",
                activated_at=now,
            )
            session.add(active)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                status = await owner_client.get("/api/v1/operations/reindex")
                assert status.status_code == 200
                body = status.json()
                assert body["purpose"] == "retrieval"
                assert body["active_generation_id"] is not None
                assert body["generations"][0]["status"] == "active"
                assert {target["name"] for target in body["targets"]} == {
                    "knowledge_revisions",
                    "document_chunks",
                    "retrieval_units",
                }
                assert body["pending_targets"] == []

                first = await owner_client.post(
                    "/api/v1/operations/reindex/enqueue",
                    headers={**owner_headers, "Idempotency-Key": "enqueue-all"},
                    json={},
                )
                assert first.status_code == 200
                assert first.json()["status"] == "enqueued"
                assert len(first.json()["targets"]) == 3

                replay = await owner_client.post(
                    "/api/v1/operations/reindex/enqueue",
                    headers={**owner_headers, "Idempotency-Key": "enqueue-all"},
                    json={},
                )
                assert replay.status_code == 200
                assert replay.json() == first.json()

                subset = await owner_client.post(
                    "/api/v1/operations/reindex/enqueue",
                    headers={**owner_headers, "Idempotency-Key": "enqueue-subset"},
                    json={"targets": ["retrieval_units"]},
                )
                assert subset.status_code == 200
                assert [target["name"] for target in subset.json()["targets"]] == ["retrieval_units"]

                unknown = await owner_client.post(
                    "/api/v1/operations/reindex/enqueue",
                    headers={**owner_headers, "Idempotency-Key": "enqueue-unknown"},
                    json={"targets": ["no-such-target"]},
                )
                assert unknown.status_code == 422

                status_after = await owner_client.get("/api/v1/operations/reindex")
                assert sorted(status_after.json()["pending_targets"]) == [
                    "document_chunks",
                    "knowledge_revisions",
                    "retrieval_units",
                ]

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": reader_session.access_token.reveal()},
            ) as reader_client:
                reader_status = await reader_client.get("/api/v1/operations/reindex")
                assert reader_status.status_code == 200
                denied = await reader_client.post(
                    "/api/v1/operations/reindex/enqueue",
                    headers={**_auth_headers(reader_session), "Idempotency-Key": "enqueue-denied"},
                    json={},
                )
                assert denied.status_code == 404
                assert denied.json()["error"]["code"] == "resource_unavailable"

            async with factory() as session:
                events = list(
                    await session.scalars(
                        select(AuditEventModel).where(
                            AuditEventModel.action == "operations.reindex.enqueue_requested"
                        )
                    )
                )
                assert len(events) == 2
        finally:
            set_session_factory(None)


async def test_promote_guards_pending_and_rollback_restores_previous(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(management_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _test_settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, reader_session = await _seed_members_and_spaces(
                session, now=now, owner_id=owner_id, reader_id=reader_id
            )
            active = EmbeddingGenerationModel(
                purpose="retrieval",
                model_id="embed-a",
                dimensions=1024,
                status="active",
                activated_at=now - timedelta(days=10),
            )
            building = EmbeddingGenerationModel(
                purpose="retrieval",
                model_id="embed-b",
                dimensions=1024,
                status="building",
            )
            retired = EmbeddingGenerationModel(
                purpose="retrieval",
                model_id="embed-old",
                dimensions=1024,
                status="retired",
                activated_at=now - timedelta(days=30),
            )
            session.add_all([active, building, retired])
            await session.flush()
            active_id = active.id
            building_id = building.id
            retired_id = retired.id
            session.add(
                MigrationBackfillRunModel(
                    name="embedding_reembed:retrieval_units",
                    phase="snapshot",
                    rows_migrated=0,
                )
            )

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                blocked = await owner_client.post(
                    f"/api/v1/operations/generations/{building_id}/promote",
                    headers={**owner_headers, "Idempotency-Key": "promote-blocked"},
                    json={},
                )
                assert blocked.status_code == 409
                assert blocked.json()["error"]["code"] == "resource_conflict"
                assert blocked.json()["error"]["details"]["pending_targets"] == ["retrieval_units"]

                forced_without_reason = await owner_client.post(
                    f"/api/v1/operations/generations/{building_id}/promote",
                    headers={**owner_headers, "Idempotency-Key": "promote-no-reason"},
                    json={"force": True},
                )
                assert forced_without_reason.status_code == 422

                forced = await owner_client.post(
                    f"/api/v1/operations/generations/{building_id}/promote",
                    headers={**owner_headers, "Idempotency-Key": "promote-forced"},
                    json={"force": True, "reason": "Measured quality gate passed on staging corpus"},
                )
                assert forced.status_code == 200
                assert forced.json() == {
                    "id": str(building_id),
                    "status": "active",
                    "previous_active_id": str(active_id),
                    "forced": True,
                }

                replay = await owner_client.post(
                    f"/api/v1/operations/generations/{building_id}/promote",
                    headers={**owner_headers, "Idempotency-Key": "promote-forced"},
                    json={"force": True, "reason": "Measured quality gate passed on staging corpus"},
                )
                assert replay.status_code == 200
                assert replay.json() == forced.json()

                status = await owner_client.get("/api/v1/operations/reindex")
                assert status.json()["active_generation_id"] == str(building_id)

                rolled_back = await owner_client.post(
                    "/api/v1/operations/generations/rollback",
                    headers={**owner_headers, "Idempotency-Key": "rollback-one"},
                    json={"reason": "Production recall regressed after promotion"},
                )
                assert rolled_back.status_code == 200
                assert rolled_back.json()["id"] == str(active_id)
                assert rolled_back.json()["previous_active_id"] == str(building_id)

                status_after = await owner_client.get("/api/v1/operations/reindex")
                assert status_after.json()["active_generation_id"] == str(active_id)

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": reader_session.access_token.reveal()},
            ) as reader_client:
                reader_headers = _auth_headers(reader_session)
                denied_promote = await reader_client.post(
                    f"/api/v1/operations/generations/{building_id}/promote",
                    headers={**reader_headers, "Idempotency-Key": "promote-denied"},
                    json={},
                )
                assert denied_promote.status_code == 404
                assert denied_promote.json()["error"]["code"] == "resource_unavailable"
                denied_rollback = await reader_client.post(
                    "/api/v1/operations/generations/rollback",
                    headers={**reader_headers, "Idempotency-Key": "rollback-denied"},
                    json={},
                )
                assert denied_rollback.status_code == 404
                assert denied_rollback.json()["error"]["code"] == "resource_unavailable"

            async with factory() as session:
                rows = {
                    row.id: row.status
                    for row in await session.scalars(select(EmbeddingGenerationModel))
                }
                assert rows[active_id] == "active"
                assert rows[building_id] == "retired"
                assert rows[retired_id] == "retired"
                promoted_events = list(
                    await session.scalars(
                        select(AuditEventModel).where(
                            AuditEventModel.action == "operations.generation.promoted"
                        )
                    )
                )
                rollback_events = list(
                    await session.scalars(
                        select(AuditEventModel).where(
                            AuditEventModel.action == "operations.generation.rolled_back"
                        )
                    )
                )
                assert len(promoted_events) == 1
                assert promoted_events[0].resource_id == str(building_id)
                assert len(rollback_events) == 1
                assert rollback_events[0].resource_id == str(active_id)
        finally:
            set_session_factory(None)


async def test_promote_happy_path_without_pending_and_no_knowledge_mutation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(management_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _test_settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, _ = await _seed_members_and_spaces(
                session, now=now, owner_id=owner_id, reader_id=reader_id
            )
            active = EmbeddingGenerationModel(
                purpose="retrieval",
                model_id="embed-a",
                dimensions=1024,
                status="active",
                activated_at=now - timedelta(days=2),
            )
            building = EmbeddingGenerationModel(
                purpose="retrieval",
                model_id="embed-b",
                dimensions=1024,
                status="building",
            )
            session.add_all([active, building])
            await session.flush()
            active_id = active.id
            building_id = building.id
            await _persist_knowledge(
                session, now=now, space_by_key={"old": "ops-space", "older": "ops-space", "fresh": "ops-space", "secret": "secret-space"}
            )
            knowledge_before = {
                (item.id, item.updated_at, item.revision, item.title)
                for item in await session.scalars(select(KnowledgeItem))
            }

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                promoted = await owner_client.post(
                    f"/api/v1/operations/generations/{building_id}/promote",
                    headers={**owner_headers, "Idempotency-Key": "promote-clean"},
                    json={},
                )
                assert promoted.status_code == 200
                assert promoted.json()["previous_active_id"] == str(active_id)

                rolled_back = await owner_client.post(
                    "/api/v1/operations/generations/rollback",
                    headers={**owner_headers, "Idempotency-Key": "rollback-clean"},
                    json={},
                )
                assert rolled_back.status_code == 200
                assert rolled_back.json()["id"] == str(active_id)

            async with factory() as session:
                knowledge_after = {
                    (item.id, item.updated_at, item.revision, item.title)
                    for item in await session.scalars(select(KnowledgeItem))
                }
                assert knowledge_before == knowledge_after
        finally:
            set_session_factory(None)


async def test_reindex_requires_operator_identity(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(management_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    settings = _test_settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=owner_id,
                    username="stale-outsider",
                    username_normalized="stale-outsider",
                    display_name="Stale Outsider",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="lonely-space", name="Lonely", created_by_member_id=owner_id))
            outsider_session = await SessionService(session).issue(
                session_principal(owner_id), now=now, step_up_at=now
            )

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": outsider_session.access_token.reveal()},
            ) as outsider_client:
                outsider_headers = _auth_headers(outsider_session)
                stale = await outsider_client.get("/api/v1/knowledge/stale")
                assert stale.status_code == 200
                assert stale.json()["items"] == []
                summary = await outsider_client.get("/api/v1/operations/summary")
                assert summary.status_code == 200
                assert summary.json()["stale_knowledge"] == {"stale_after_days": 90, "stale_items": 0}
                denied = await outsider_client.post(
                    "/api/v1/operations/reindex/enqueue",
                    headers={**outsider_headers, "Idempotency-Key": "enqueue-outsider"},
                    json={},
                )
                assert denied.status_code == 404
                assert denied.json()["error"]["code"] == "resource_unavailable"
        finally:
            set_session_factory(None)


@pytest.mark.asyncio
async def test_stale_after_days_defaults_to_ninety() -> None:
    assert Settings(gateway={}).gateway.stale_after_days == 90
