from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
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
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, Workspace
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.application.services import authorized_retrieval_service as authorized_retrieval_module
from src.gateway.presentation.routers import management as management_module
from src.gateway.presentation.routers.management import router
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


def principal(member_id, system_role=SystemRole.MEMBER):
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


def _settings(tmp_path):
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


async def _seed(session, now, owner_id, reader_id):
    session.add_all(
        [
            MemberModel(
                id=owner_id,
                username="lifecycle-owner",
                username_normalized="lifecycle-owner",
                display_name="Lifecycle Owner",
                status=MemberStatus.ACTIVE.value,
                system_role=SystemRole.SUPER_ADMIN.value,
                force_password_change=False,
            ),
            MemberModel(
                id=reader_id,
                username="lifecycle-reader",
                username_normalized="lifecycle-reader",
                display_name="Lifecycle Reader",
                status=MemberStatus.ACTIVE.value,
                system_role=SystemRole.MEMBER.value,
                force_password_change=False,
            ),
            Workspace(id="ops-space", name="Ops", created_by_member_id=owner_id),
        ]
    )
    await session.flush()
    session.add_all(
        [
            SpaceMembershipModel(id=uuid4(), space_id="ops-space", member_id=owner_id, role=SpaceRole.OWNER.value),
            SpaceMembershipModel(id=uuid4(), space_id="ops-space", member_id=reader_id, role=SpaceRole.READER.value),
        ]
    )
    session.add(
        EmbeddingGenerationModel(
            purpose="retrieval",
            model_id="embed-test",
            dimensions=EMBED_DIM,
            status="active",
            activated_at=now,
        )
    )
    owner_session = await SessionService(session).issue(
        principal(owner_id, SystemRole.SUPER_ADMIN), now=now, step_up_at=now
    )
    reader_session = await SessionService(session).issue(principal(reader_id), now=now, step_up_at=now)
    return owner_session, reader_session


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


async def test_lifecycle_create_defaults_and_transition_graph(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, reader_session = await _seed(session, now=now, owner_id=owner_id, reader_id=reader_id)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                created = await owner_client.post(
                    "/api/v1/knowledge",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-default"},
                    json={"space_id": "ops-space", "title": "Default item", "content": "Plain content.", "tags": []},
                )
                assert created.status_code == 201
                body = created.json()
                assert body["lifecycle_status"] == "accepted"
                assert body["origin"] is None
                assert body["expires_at"] is None
                item_id = body["id"]

                bad_status = await owner_client.post(
                    "/api/v1/knowledge",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-bad-status"},
                    json={
                        "space_id": "ops-space",
                        "title": "Bad",
                        "content": "Bad content.",
                        "tags": [],
                        "lifecycle_status": "superseded",
                    },
                )
                assert bad_status.status_code == 422

                observed = await owner_client.post(
                    "/api/v1/knowledge",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-observed"},
                    json={
                        "space_id": "ops-space",
                        "title": "Agent hunch",
                        "content": "Unverified guess.",
                        "tags": [],
                        "lifecycle_status": "observation",
                        "origin": "agent-memory",
                        "source_detail": "session abc",
                        "expires_at": (now + timedelta(days=30)).isoformat(),
                    },
                )
                assert observed.status_code == 201
                observed_body = observed.json()
                assert observed_body["lifecycle_status"] == "observation"
                assert observed_body["origin"] == "agent-memory"
                assert observed_body["expires_at"] is not None
                observed_id = observed_body["id"]
                observed_version = observed_body["version"]

                illegal = await owner_client.post(
                    f"/api/v1/knowledge/{observed_id}/transitions",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-illegal"},
                    json={"to_status": "superseded", "expected_version": observed_version, "review_note": "Skip"},
                )
                assert illegal.status_code == 409
                assert illegal.json()["error"]["code"] == "resource_conflict"

                to_candidate = await owner_client.post(
                    f"/api/v1/knowledge/{observed_id}/transitions",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-to-candidate"},
                    json={"to_status": "candidate", "expected_version": observed_version},
                )
                assert to_candidate.status_code == 200
                assert to_candidate.json() == {
                    "id": observed_id,
                    "from_status": "observation",
                    "to_status": "candidate",
                    "version": observed_version,
                }

                stale_version = await owner_client.post(
                    f"/api/v1/knowledge/{observed_id}/transitions",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-stale"},
                    json={"to_status": "accepted", "expected_version": observed_version + 99},
                )
                assert stale_version.status_code == 409
                assert stale_version.json()["error"]["code"] == "version_mismatch"

                replay = await owner_client.post(
                    f"/api/v1/knowledge/{observed_id}/transitions",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-to-candidate"},
                    json={"to_status": "candidate", "expected_version": observed_version},
                )
                assert replay.status_code == 200
                assert replay.json() == to_candidate.json()

                to_accepted = await owner_client.post(
                    f"/api/v1/knowledge/{observed_id}/transitions",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-to-accepted"},
                    json={"to_status": "accepted", "expected_version": observed_version, "review_note": "Verified"},
                )
                assert to_accepted.status_code == 200
                assert to_accepted.json()["to_status"] == "accepted"

                no_note = await owner_client.post(
                    f"/api/v1/knowledge/{observed_id}/transitions",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-no-note"},
                    json={"to_status": "superseded", "expected_version": observed_version},
                )
                assert no_note.status_code == 422

                detail = await owner_client.get(f"/api/v1/knowledge/{observed_id}")
                assert detail.status_code == 200
                assert detail.json()["lifecycle_status"] == "accepted"
                assert detail.json()["review_note"] == "Verified"

            async with factory() as session:
                events = list(
                    await session.scalars(
                        select(AuditEventModel).where(AuditEventModel.action == "knowledge.lifecycle.transitioned")
                    )
                )
                assert len(events) == 2
        finally:
            set_session_factory(None)


async def test_superseded_excluded_from_search_but_evidence_resolves(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, _ = await _seed(session, now=now, owner_id=owner_id, reader_id=reader_id)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                created = await owner_client.post(
                    "/api/v1/knowledge",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-search-item"},
                    json={
                        "space_id": "ops-space",
                        "title": "Torque specification",
                        "content": "Torque the valve to forty newton meters.",
                        "tags": [],
                    },
                )
                assert created.status_code == 201
                item_id = created.json()["id"]
                version = created.json()["version"]

                before = await owner_client.post(
                    "/api/v1/retrieval/search",
                    headers=owner_headers,
                    json={"query": "torque valve", "space_ids": ["ops-space"], "limit": 10},
                )
                assert before.status_code == 200
                assert item_id in [hit["canonical_id"] for hit in before.json()["hits"]]

                supersede = await owner_client.post(
                    f"/api/v1/knowledge/{item_id}/transitions",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-supersede-search"},
                    json={"to_status": "superseded", "expected_version": version, "review_note": "Replaced"},
                )
                assert supersede.status_code == 200

                after = await owner_client.post(
                    "/api/v1/retrieval/search",
                    headers=owner_headers,
                    json={"query": "torque valve", "space_ids": ["ops-space"], "limit": 10},
                )
                assert after.status_code == 200
                assert item_id not in [hit["canonical_id"] for hit in after.json()["hits"]]

                listing = await owner_client.get("/api/v1/knowledge?space_id=ops-space&lifecycle_status=superseded")
                assert listing.status_code == 200
                assert [item["id"] for item in listing.json()["items"]] == [item_id]

                detail = await owner_client.get(f"/api/v1/knowledge/{item_id}")
                assert detail.status_code == 200

                resolve = await owner_client.post(
                    "/api/v1/evidence/resolve",
                    headers=owner_headers,
                    json={"citation_uri": before.json()["hits"][0]["citation_uri"]},
                )
                assert resolve.status_code == 200
                assert resolve.json()["superseded"] is True
                assert resolve.json()["canonical_id"] == item_id

                reaccept = await owner_client.post(
                    f"/api/v1/knowledge/{item_id}/transitions",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-reaccept"},
                    json={"to_status": "accepted", "expected_version": version},
                )
                assert reaccept.status_code == 200

                restored = await owner_client.post(
                    "/api/v1/retrieval/search",
                    headers=owner_headers,
                    json={"query": "torque valve", "space_ids": ["ops-space"], "limit": 10},
                )
                assert restored.status_code == 200
                assert item_id in [hit["canonical_id"] for hit in restored.json()["hits"]]
        finally:
            set_session_factory(None)


async def test_lifecycle_transition_requires_write_and_export_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, reader_session = await _seed(session, now=now, owner_id=owner_id, reader_id=reader_id)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                created = await owner_client.post(
                    "/api/v1/knowledge",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-export-item"},
                    json={
                        "space_id": "ops-space",
                        "title": "Export me",
                        "content": "Round trip content.",
                        "tags": [],
                        "lifecycle_status": "candidate",
                        "origin": "import-test",
                    },
                )
                assert created.status_code == 201
                item_id = created.json()["id"]
                version = created.json()["version"]

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": reader_session.access_token.reveal()},
            ) as reader_client:
                reader_headers = _auth_headers(reader_session)
                denied = await reader_client.post(
                    f"/api/v1/knowledge/{item_id}/transitions",
                    headers={**reader_headers, "Idempotency-Key": "lifecycle-denied"},
                    json={"to_status": "accepted", "expected_version": version},
                )
                assert denied.status_code in (403, 404)

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                exported = await owner_client.get("/api/v1/knowledge/export", params={"space_id": "ops-space"})
                assert exported.status_code == 200
                document = exported.json()
                exported_item = next(item for item in document["items"] if item["id"] == item_id)
                assert exported_item["lifecycle_status"] == "candidate"
                assert exported_item["origin"] == "import-test"

                document["space_id"] = "ops-space"
                for item in document["items"]:
                    if item["id"] != item_id:
                        item["id"] = str(uuid4())
                        for revision in item["revisions"]:
                            revision["id"] = str(uuid4())
                reimport = await owner_client.post(
                    "/api/v1/knowledge/import",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-reimport"},
                    params={"space_id": "ops-space"},
                    json=document,
                )
                assert reimport.status_code == 200
        finally:
            set_session_factory(None)


async def test_transition_replay_denied_after_membership_removed_and_overlong_import_rejected(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, _ = await _seed(session, now=now, owner_id=owner_id, reader_id=reader_id)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                created = await owner_client.post(
                    "/api/v1/knowledge",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-revoke-item"},
                    json={"space_id": "ops-space", "title": "Revoke probe", "content": "Probe content.", "tags": []},
                )
                assert created.status_code == 201
                item_id = created.json()["id"]
                version = created.json()["version"]

                first = await owner_client.post(
                    f"/api/v1/knowledge/{item_id}/transitions",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-revoke-key"},
                    json={"to_status": "superseded", "expected_version": version, "review_note": "Lease ended"},
                )
                assert first.status_code == 200

            async with factory.begin() as session:
                membership = await session.scalar(
                    select(SpaceMembershipModel).where(
                        SpaceMembershipModel.space_id == "ops-space",
                        SpaceMembershipModel.member_id == owner_id,
                    )
                )
                assert membership is not None
                await session.delete(membership)

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                replay = await owner_client.post(
                    f"/api/v1/knowledge/{item_id}/transitions",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-revoke-key"},
                    json={"to_status": "superseded", "expected_version": version, "review_note": "Lease ended"},
                )
                assert replay.status_code in (403, 404)
        finally:
            set_session_factory(None)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, _ = await _seed(session, now=now, owner_id=owner_id, reader_id=reader_id)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                document = {
                    "format": "openknowledge-knowledge-export",
                    "version": 1,
                    "space_id": "ops-space",
                    "items": [
                        {
                            "id": str(uuid4()),
                            "title": "Overlong",
                            "tags": [],
                            "lifecycle_status": "accepted",
                            "origin": "x" * 201,
                            "source_detail": None,
                            "review_note": None,
                            "expires_at": None,
                            "revisions": [
                                {
                                    "id": str(uuid4()),
                                    "version": 1,
                                    "title": "Overlong",
                                    "content": "Content.",
                                    "tags": [],
                                }
                            ],
                        }
                    ],
                }
                rejected = await owner_client.post(
                    "/api/v1/knowledge/import",
                    headers={**owner_headers, "Idempotency-Key": "lifecycle-overlong"},
                    params={"space_id": "ops-space"},
                    json=document,
                )
                assert rejected.status_code == 422
        finally:
            set_session_factory(None)
