import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

from src.gateway.application.services import knowledge_management_service as knowledge_management_module
from src.gateway.application.services.bounded_document_parser import BoundedDocumentParser
from src.gateway.application.services.document_ingestion_worker import DocumentIngestionWorker
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence import retrieval_unit_repository as retrieval_unit_repository_module
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import (
    DocumentModel,
    DocumentRevisionModel,
    EmbeddingGenerationModel,
)
from src.gateway.infrastructure.persistence.models import EMBED_DIM, Workspace
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from src.gateway.observability import metrics_registry_context
from src.gateway.presentation.metrics import MetricsRegistry
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.application.services import authorized_retrieval_service as authorized_retrieval_module
from src.gateway.presentation.routers import management as management_module
from src.gateway.presentation.routers.management import router
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary is required")


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
                username="git-owner",
                username_normalized="git-owner",
                display_name="Git Owner",
                status=MemberStatus.ACTIVE.value,
                system_role=SystemRole.SUPER_ADMIN.value,
                force_password_change=False,
            ),
            MemberModel(
                id=reader_id,
                username="git-reader",
                username_normalized="git-reader",
                display_name="Git Reader",
                status=MemberStatus.ACTIVE.value,
                system_role=SystemRole.MEMBER.value,
                force_password_change=False,
            ),
            Workspace(id="git-space", name="Git", created_by_member_id=owner_id),
            Workspace(id="other-space", name="Other", created_by_member_id=owner_id),
        ]
    )
    await session.flush()
    session.add_all(
        [
            SpaceMembershipModel(id=uuid4(), space_id="git-space", member_id=owner_id, role=SpaceRole.OWNER.value),
            SpaceMembershipModel(id=uuid4(), space_id="other-space", member_id=owner_id, role=SpaceRole.OWNER.value),
            SpaceMembershipModel(id=uuid4(), space_id="git-space", member_id=reader_id, role=SpaceRole.READER.value),
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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _fixture_repo(path: Path) -> Path:
    repo = path / "fixture-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "guide.md").write_text("# Guide\n\nTorque the valve.\n", encoding="utf-8")
    (repo / "notes.txt").write_text("Check pressure daily.\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


@needs_git
async def test_git_connector_register_sync_deltas_and_unregister(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _settings(tmp_path)
    repo = _fixture_repo(tmp_path)

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
                registered = await owner_client.post(
                    "/api/v1/sources/connectors",
                    headers={**owner_headers, "Idempotency-Key": "git-register"},
                    json={"space_id": "git-space", "repo": str(repo), "branch": "main"},
                )
                assert registered.status_code == 201
                connector_id = registered.json()["id"]
                assert registered.json()["branch"] == "main"
                assert registered.json()["last_synced_commit"] is None

                replay = await owner_client.post(
                    "/api/v1/sources/connectors",
                    headers={**owner_headers, "Idempotency-Key": "git-register"},
                    json={"space_id": "git-space", "repo": str(repo), "branch": "main"},
                )
                assert replay.status_code == 201
                assert replay.json()["id"] == connector_id

                first = await owner_client.post(
                    f"/api/v1/sources/connectors/{connector_id}/sync",
                    headers={**owner_headers, "Idempotency-Key": "git-sync-1"},
                    json={},
                )
                assert first.status_code == 202
                assert first.json()["enqueued"] == 2
                assert first.json()["archived"] == 0
                head = first.json()["commit"]

                listed = await owner_client.get("/api/v1/sources/connectors", params={"space_id": "git-space"})
                assert listed.status_code == 200
                assert listed.json()["items"][0]["last_synced_commit"] == head

                second = await owner_client.post(
                    f"/api/v1/sources/connectors/{connector_id}/sync",
                    headers={**owner_headers, "Idempotency-Key": "git-sync-2"},
                    json={},
                )
                assert second.status_code == 202
                assert second.json()["commit"] == head
                assert second.json()["enqueued"] == 0
                assert second.json()["archived"] == 0

                (repo / "guide.md").write_text("# Guide\n\nTorque gently.\n", encoding="utf-8")
                _git(repo, "add", ".")
                _git(repo, "commit", "-m", "tweak before worker runs")

                pending_sync = await owner_client.post(
                    f"/api/v1/sources/connectors/{connector_id}/sync",
                    headers={**owner_headers, "Idempotency-Key": "git-sync-pending"},
                    json={},
                )
                assert pending_sync.status_code == 202
                assert pending_sync.json()["enqueued"] == 0
                assert pending_sync.json()["skipped"] == 1
                assert any("pending" in reason and "guide.md" in reason for reason in pending_sync.json()["skipped_reasons"])

                worker = DocumentIngestionWorker(
                    factory,
                    LocalVersionedObjectStorage(tmp_path / "storage"),
                    BoundedDocumentParser(timeout_seconds=30, memory_limit_bytes=256 * 1024 * 1024),
                    StubEmbeddingClient(),
                    worker_id="git-connector-worker",
                    lease_seconds=30,
                    heartbeat_interval_seconds=1,
                    chunk_size=2000,
                    chunk_overlap=200,
                    max_chunks=10000,
                )
                registry = MetricsRegistry()
                token = metrics_registry_context.set(registry)
                try:
                    for _ in range(10):
                        if await worker.run_once() is None:
                            break
                finally:
                    metrics_registry_context.reset(token)

                (repo / "guide.md").write_text("# Guide\n\nTorque the valve to forty newton meters.\n", encoding="utf-8")
                (repo / "notes.txt").unlink()
                (repo / "runbook.md").write_text("# Runbook\n\nIsolate before service.\n", encoding="utf-8")
                (repo / "firmware.bin").write_bytes(bytes(range(256)))
                _git(repo, "add", ".")
                _git(repo, "commit", "-m", "revise")

                third = await owner_client.post(
                    f"/api/v1/sources/connectors/{connector_id}/sync",
                    headers={**owner_headers, "Idempotency-Key": "git-sync-3"},
                    json={},
                )
                assert third.status_code == 202
                assert third.json()["enqueued"] == 2
                assert third.json()["archived"] == 1
                assert third.json()["skipped"] == 1
                assert any("firmware.bin" in reason for reason in third.json()["skipped_reasons"])

                token = metrics_registry_context.set(MetricsRegistry())
                try:
                    for _ in range(10):
                        if await worker.run_once() is None:
                            break
                finally:
                    metrics_registry_context.reset(token)

                async with factory() as session:
                    guides = list(
                        await session.scalars(
                            select(DocumentModel).where(
                                DocumentModel.space_id == "git-space",
                                DocumentModel.display_name == "guide.md",
                                DocumentModel.archived_at.is_(None),
                            )
                        )
                    )
                    assert len(guides) == 1
                    revisions = list(
                        await session.scalars(
                            select(DocumentRevisionModel)
                            .where(DocumentRevisionModel.document_id == guides[0].id)
                            .order_by(DocumentRevisionModel.version.asc())
                        )
                    )
                    assert [revision.version for revision in revisions] == [1, 2]
                    assert guides[0].current_revision_id == revisions[-1].id

                removed = await owner_client.get("/api/v1/sources/connectors", params={"space_id": "git-space"})
                assert removed.status_code == 200

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": reader_session.access_token.reveal()},
            ) as reader_client:
                denied = await reader_client.post(
                    "/api/v1/sources/connectors",
                    headers={**_auth_headers(reader_session), "Idempotency-Key": "git-denied"},
                    json={"space_id": "git-space", "repo": str(repo), "branch": "main"},
                )
                assert denied.status_code in (403, 404)

            async with factory() as session:
                events = list(
                    await session.scalars(
                        select(AuditEventModel).where(AuditEventModel.action == "connector.synced")
                    )
                )
                assert len(events) == 4

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as owner_client:
                owner_headers = _auth_headers(owner_session)
                unregistered = await owner_client.delete(
                    f"/api/v1/sources/connectors/{connector_id}",
                    headers={**owner_headers, "Idempotency-Key": "git-unregister"},
                )
                assert unregistered.status_code == 204
                listed = await owner_client.get("/api/v1/sources/connectors", params={"space_id": "git-space"})
                assert listed.json()["items"] == []
                sources = await owner_client.get("/api/v1/sources", params={"space_id": "git-space"})
                assert sources.status_code == 200
                assert len(sources.json()["items"]) >= 2
        finally:
            set_session_factory(None)


@needs_git
async def test_git_connector_rejects_invalid_repo_and_branch(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    reader_id = uuid4()
    settings = _settings(tmp_path)
    repo = _fixture_repo(tmp_path)

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
                missing = await owner_client.post(
                    "/api/v1/sources/connectors",
                    headers={**owner_headers, "Idempotency-Key": "git-missing"},
                    json={"space_id": "git-space", "repo": str(tmp_path / "nope"), "branch": "main"},
                )
                assert missing.status_code == 422

                bad_branch = await owner_client.post(
                    "/api/v1/sources/connectors",
                    headers={**owner_headers, "Idempotency-Key": "git-bad-branch"},
                    json={"space_id": "git-space", "repo": str(repo), "branch": "no-such-branch"},
                )
                assert bad_branch.status_code == 422

                cross_space = await owner_client.post(
                    "/api/v1/sources/connectors",
                    headers={**owner_headers, "Idempotency-Key": "git-cross"},
                    json={"space_id": "unknown-space", "repo": str(repo), "branch": "main"},
                )
                assert cross_space.status_code in (403, 404)
        finally:
            set_session_factory(None)
