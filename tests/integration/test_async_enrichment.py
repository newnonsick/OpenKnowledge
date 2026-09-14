from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

from src.gateway.application.services import knowledge_management_service as knowledge_management_module
from src.gateway.application.services.document_ingestion_worker import DocumentIngestionWorker
from src.gateway.application.services.ingestion_job_service import IngestionJobService
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence import retrieval_unit_repository as retrieval_unit_repository_module
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, IngestionJobModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, Workspace
from src.gateway.observability import metrics_registry_context
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.metrics import MetricsRegistry
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
    dimension = EMBED_DIM

    async def embed_query(self, query):
        return [0.1] * EMBED_DIM

    async def embed_texts(self, texts):
        return [[0.1] * EMBED_DIM for _ in texts]


class FailingEmbeddingClient(StubEmbeddingClient):
    async def embed_texts(self, texts):
        raise RuntimeError("provider down")


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


async def _seed(session, now, owner_id):
    session.add(
        MemberModel(
            id=owner_id,
            username="enrich-owner",
            username_normalized="enrich-owner",
            display_name="Enrich Owner",
            status=MemberStatus.ACTIVE.value,
            system_role=SystemRole.SUPER_ADMIN.value,
            force_password_change=False,
        )
    )
    session.add(Workspace(id="enrich-space", name="Enrich", created_by_member_id=owner_id))
    await session.flush()
    session.add(
        SpaceMembershipModel(id=uuid4(), space_id="enrich-space", member_id=owner_id, role=SpaceRole.OWNER.value)
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
    await session.flush()
    return await SessionService(session).issue(
        principal(owner_id, SystemRole.SUPER_ADMIN), now=now, step_up_at=now
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


async def _run_worker_once(factory):
    worker = DocumentIngestionWorker(
        factory,
        storage=None,
        parser=None,
        embedding_client=StubEmbeddingClient(),
        worker_id="enrich-worker",
        lease_seconds=30,
        heartbeat_interval_seconds=1,
        chunk_size=2000,
        chunk_overlap=200,
        max_chunks=10000,
    )
    registry = MetricsRegistry()
    token = metrics_registry_context.set(registry)
    try:
        return await worker.run_once()
    finally:
        metrics_registry_context.reset(token)


async def test_async_create_returns_202_with_lexical_read_before_enrichment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: FailingEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session = await _seed(session, now=now, owner_id=owner_id)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as client:
                headers = _auth_headers(owner_session)
                created = await client.post(
                    "/api/v1/knowledge",
                    headers={**headers, "Idempotency-Key": "enrich-create"},
                    json={
                        "space_id": "enrich-space",
                        "title": "Async item",
                        "content": "Lexical content is committed immediately.",
                        "tags": [],
                        "enrich_async": True,
                    },
                )
                assert created.status_code == 202
                body = created.json()
                assert body["enrichment"] == "pending"
                assert body["job_state"] == "queued"
                job_id = UUID(body["job_id"])
                item_id = body["id"]

                fetched = await client.get(f"/api/v1/knowledge/{item_id}")
                assert fetched.status_code == 200
                assert fetched.json()["content"] == "Lexical content is committed immediately."
                assert fetched.json()["enrichment"] == "pending"

                listed = await client.get("/api/v1/knowledge", params={"space_id": "enrich-space"})
                assert listed.status_code == 200
                assert any(entry["id"] == item_id for entry in listed.json()["items"])

                jobs = await client.get("/api/v1/ingestion-jobs", params={"space_id": "enrich-space", "state": "queued"})
                assert jobs.status_code == 200
                assert any(entry["id"] == str(job_id) for entry in jobs.json()["items"])

                async with factory() as session:
                    job = await session.get(IngestionJobModel, job_id)
                    assert job is not None
                    assert job.job_type == "knowledge_enrichment"
                    assert job.document_id is None
                    assert job.knowledge_item_id == UUID(item_id)

                assert await _run_worker_once(factory) == job_id

                refetched = await client.get(f"/api/v1/knowledge/{item_id}")
                assert refetched.status_code == 200
                assert refetched.json()["enrichment"] == "enriched"

                async with factory() as session:
                    job = await session.get(IngestionJobModel, job_id)
                    assert job is not None
                    units = list(
                        await session.scalars(
                            select(RetrievalUnitModel).where(
                                RetrievalUnitModel.knowledge_revision_id == job.knowledge_revision_id,
                                RetrievalUnitModel.active.is_(True),
                            )
                        )
                    )
                    assert len(units) == 1

                searched = await client.post(
                    "/api/v1/retrieval/search",
                    headers=headers,
                    json={"query": "Lexical content", "space_ids": ["enrich-space"], "limit": 5},
                )
                assert searched.status_code == 200
                assert item_id in [hit["canonical_id"] for hit in searched.json()["hits"]]
        finally:
            set_session_factory(None)


async def test_sync_default_unchanged_and_failed_enrichment_retries(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session = await _seed(session, now=now, owner_id=owner_id)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as client:
                headers = _auth_headers(owner_session)
                created = await client.post(
                    "/api/v1/knowledge",
                    headers={**headers, "Idempotency-Key": "enrich-sync"},
                    json={"space_id": "enrich-space", "title": "Sync item", "content": "Sync content.", "tags": []},
                )
                assert created.status_code == 201
                body = created.json()
                assert body["enrichment"] is None
                assert "job_id" not in body

                fetched = await client.get(f"/api/v1/knowledge/{body['id']}")
                assert fetched.json()["enrichment"] is None

                async_created = await client.post(
                    "/api/v1/knowledge",
                    headers={**headers, "Idempotency-Key": "enrich-will-fail"},
                    json={
                        "space_id": "enrich-space",
                        "title": "Failing item",
                        "content": "Will fail then retry.",
                        "tags": [],
                        "enrich_async": True,
                    },
                )
                assert async_created.status_code == 202
                failing_job = UUID(async_created.json()["job_id"])
                failing_item = async_created.json()["id"]

                cancellable = await client.post(
                    "/api/v1/knowledge",
                    headers={**headers, "Idempotency-Key": "enrich-will-cancel"},
                    json={
                        "space_id": "enrich-space",
                        "title": "Cancellable item",
                        "content": "Cancelled before enrichment.",
                        "tags": [],
                        "enrich_async": True,
                    },
                )
                assert cancellable.status_code == 202
                cancellable_job = UUID(cancellable.json()["job_id"])
                cancelled = await client.post(
                    f"/api/v1/ingestion-jobs/{cancellable_job}/cancel",
                    headers={**headers, "Idempotency-Key": "enrich-cancel"},
                )
                assert cancelled.status_code == 200
                assert cancelled.json()["state"] == "cancellation_requested"

                worker = DocumentIngestionWorker(
                    factory,
                    storage=None,
                    parser=None,
                    embedding_client=FailingEmbeddingClient(),
                    worker_id="enrich-fail-worker",
                    lease_seconds=30,
                    heartbeat_interval_seconds=1,
                    chunk_size=2000,
                    chunk_overlap=200,
                    max_chunks=10000,
                )
                async with factory.begin() as session:
                    doomed = await session.get(IngestionJobModel, failing_job)
                    doomed.max_attempts = 1
                async with factory.begin() as session:
                    claim = await IngestionJobService(session).claim_next("enrich-fail-worker", lease_seconds=30)
                assert claim is not None and claim.job_id == failing_job
                outcome = await worker._process_claim(claim)
                assert outcome == "failed"

                async with factory() as session:
                    job = await session.get(IngestionJobModel, failing_job)
                    assert job.state == "failed"

                retried = await client.post(
                    f"/api/v1/ingestion-jobs/{failing_job}/retry",
                    headers={**headers, "Idempotency-Key": "enrich-retry"},
                )
                assert retried.status_code == 200
                assert retried.json()["state"] == "retry_requested"

                still_readable = await client.get(f"/api/v1/knowledge/{failing_item}")
                assert still_readable.status_code == 200
                assert still_readable.json()["content"] == "Will fail then retry."
        finally:
            set_session_factory(None)


async def test_async_update_returns_202_and_tracks_revision(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session = await _seed(session, now=now, owner_id=owner_id)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as client:
                headers = _auth_headers(owner_session)
                created = await client.post(
                    "/api/v1/knowledge",
                    headers={**headers, "Idempotency-Key": "enrich-update-base"},
                    json={"space_id": "enrich-space", "title": "Base", "content": "Base content.", "tags": []},
                )
                assert created.status_code == 201
                item_id = created.json()["id"]
                version = created.json()["version"]

                updated = await client.put(
                    f"/api/v1/knowledge/{item_id}",
                    headers={**headers, "Idempotency-Key": "enrich-update-async"},
                    json={
                        "expected_version": version,
                        "title": "Base revised",
                        "content": "Revised content.",
                        "tags": [],
                        "enrich_async": True,
                    },
                )
                assert updated.status_code == 202
                assert updated.json()["enrichment"] == "pending"
                assert updated.json()["job_state"] == "queued"
                assert await _run_worker_once(factory) == UUID(updated.json()["job_id"])

                fetched = await client.get(f"/api/v1/knowledge/{item_id}")
                assert fetched.json()["content"] == "Revised content."
                assert fetched.json()["enrichment"] == "enriched"
        finally:
            set_session_factory(None)
