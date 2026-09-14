from datetime import datetime, timezone
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from src.gateway.application.services import knowledge_management_service as knowledge_management_module
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence import retrieval_unit_repository as retrieval_unit_repository_module
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
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


async def _seed(session, now, owner_id, editor_id):
    session.add_all(
        [
            MemberModel(
                id=owner_id,
                username="chunk-owner",
                username_normalized="chunk-owner",
                display_name="Chunk Owner",
                status=MemberStatus.ACTIVE.value,
                system_role=SystemRole.SUPER_ADMIN.value,
                force_password_change=False,
            ),
            MemberModel(
                id=editor_id,
                username="chunk-editor",
                username_normalized="chunk-editor",
                display_name="Chunk Editor",
                status=MemberStatus.ACTIVE.value,
                system_role=SystemRole.MEMBER.value,
                force_password_change=False,
            ),
            Workspace(id="chunk-space", name="Chunk", created_by_member_id=owner_id),
        ]
    )
    await session.flush()
    session.add_all(
        [
            SpaceMembershipModel(id=uuid4(), space_id="chunk-space", member_id=owner_id, role=SpaceRole.OWNER.value),
            SpaceMembershipModel(id=uuid4(), space_id="chunk-space", member_id=editor_id, role=SpaceRole.EDITOR.value),
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
    await session.flush()
    owner_session = await SessionService(session).issue(
        principal(owner_id, SystemRole.SUPER_ADMIN), now=now, step_up_at=now
    )
    editor_session = await SessionService(session).issue(principal(editor_id), now=now, step_up_at=now)
    return owner_session, editor_session


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


async def test_chunk_policy_override_fallback_and_422(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    editor_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, editor_session = await _seed(session, now=now, owner_id=owner_id, editor_id=editor_id)

        app = _build_app(settings, factory)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": owner_session.access_token.reveal()},
            ) as client:
                headers = _auth_headers(owner_session)
                detail = await client.get("/api/v1/spaces/chunk-space")
                assert detail.status_code == 200
                fallback = detail.json()["chunk_policy"]
                assert fallback["source"] == "global"
                assert fallback["chunk_size"] == 2000
                assert fallback["chunk_overlap"] == 200
                assert fallback["chunk_strategy"] == "semantic"

                updated = await client.put(
                    "/api/v1/spaces/chunk-space/chunk-policy",
                    headers={**headers, "Idempotency-Key": "chunk-set"},
                    json={"chunk_size": 500, "chunk_overlap": 50, "chunk_strategy": "fixed"},
                )
                assert updated.status_code == 200
                policy = updated.json()["chunk_policy"]
                assert policy == {"chunk_size": 500, "chunk_overlap": 50, "chunk_strategy": "fixed", "source": "space"}

                refetched = await client.get("/api/v1/spaces/chunk-space")
                assert refetched.json()["chunk_policy"]["source"] == "space"
                assert refetched.json()["chunk_policy"]["chunk_size"] == 500

                bad_overlap = await client.put(
                    "/api/v1/spaces/chunk-space/chunk-policy",
                    headers={**headers, "Idempotency-Key": "chunk-bad-overlap"},
                    json={"chunk_size": 500, "chunk_overlap": 500, "chunk_strategy": "fixed"},
                )
                assert bad_overlap.status_code == 422

                bad_strategy = await client.put(
                    "/api/v1/spaces/chunk-space/chunk-policy",
                    headers={**headers, "Idempotency-Key": "chunk-bad-strategy"},
                    json={"chunk_size": 500, "chunk_overlap": 50, "chunk_strategy": "sliding"},
                )
                assert bad_strategy.status_code == 422

                tiny = await client.put(
                    "/api/v1/spaces/chunk-space/chunk-policy",
                    headers={**headers, "Idempotency-Key": "chunk-tiny"},
                    json={"chunk_size": 8, "chunk_overlap": 2, "chunk_strategy": "fixed"},
                )
                assert tiny.status_code == 422

                unknown = await client.put(
                    "/api/v1/spaces/chunk-space/chunk-policy",
                    headers={**headers, "Idempotency-Key": "chunk-unknown-field"},
                    json={"chunk_size": 500, "chunk_overlap": 50, "chunk_strategy": "fixed", "reranker": True},
                )
                assert unknown.status_code == 422

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": editor_session.access_token.reveal()},
            ) as editor_client:
                denied = await editor_client.put(
                    "/api/v1/spaces/chunk-space/chunk-policy",
                    headers={**_auth_headers(editor_session), "Idempotency-Key": "chunk-editor-denied"},
                    json={"chunk_size": 600, "chunk_overlap": 60, "chunk_strategy": "semantic"},
                )
                assert denied.status_code in (403, 404)
        finally:
            set_session_factory(None)


async def test_effective_policy_resolution_prefers_space_override(tmp_path) -> None:
    from src.gateway.application.services.chunk_policy_service import effective_chunk_policy
    from src.gateway.infrastructure.persistence.models import Workspace as WorkspaceModel

    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=owner_id,
                    username="policy-owner",
                    username_normalized="policy-owner",
                    display_name="Policy Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(WorkspaceModel(id="policy-space", name="Policy", created_by_member_id=owner_id))
            await session.flush()

        async with factory() as session:
            resolved = await effective_chunk_policy(session, "policy-space")
            assert resolved.source == "global"
            assert (resolved.chunk_size, resolved.chunk_overlap, resolved.chunk_strategy) == (2000, 200, "semantic")

        async with factory.begin() as session:
            space = await session.get(WorkspaceModel, "policy-space")
            space.chunk_size = 400
            space.chunk_overlap = 40
            space.chunk_strategy = "fixed"

        async with factory() as session:
            resolved = await effective_chunk_policy(session, "policy-space")
            assert resolved.source == "space"
            assert (resolved.chunk_size, resolved.chunk_overlap, resolved.chunk_strategy) == (400, 40, "fixed")


async def test_worker_chunks_upload_with_space_override(tmp_path, monkeypatch) -> None:
    from sqlalchemy import select as sa_select

    from src.gateway.application.services.bounded_document_parser import ParsedDocument
    from src.gateway.application.services.document_ingestion_worker import DocumentIngestionWorker
    from src.gateway.application.services.document_upload_service import DocumentUploadService
    from src.gateway.infrastructure.persistence.ingestion_models import DocumentRevisionChunkModel
    from src.gateway.infrastructure.persistence.models import Workspace as WorkspaceModel
    from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
    from src.gateway.observability import metrics_registry_context
    from src.gateway.presentation.metrics import MetricsRegistry

    monkeypatch.setattr(knowledge_management_module, "default_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(authorized_retrieval_module, "default_retrieval_embedding_client", lambda: StubEmbeddingClient())
    monkeypatch.setattr(retrieval_unit_repository_module, "EMBED_DIM", EMBED_DIM)
    now = datetime.now(timezone.utc)
    owner_id = uuid4()
    settings = _settings(tmp_path)
    text = "x" * 200

    class _TextParser:
        async def parse(self, *, filename: str, mime_type: str, content: bytes) -> ParsedDocument:
            return ParsedDocument(text=content.decode(), parser_version="text-test-v1")

    async def _chunks(value: bytes):
        yield value

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            owner_session, _ = await _seed(session, now=now, owner_id=owner_id, editor_id=uuid4())

        async with factory.begin() as session:
            space = await session.get(WorkspaceModel, "chunk-space")
            space.chunk_size = 64
            space.chunk_overlap = 8
            space.chunk_strategy = "fixed"

        storage = LocalVersionedObjectStorage(tmp_path / "storage")
        receipt = await DocumentUploadService(
            factory, storage, max_upload_bytes=1024 * 1024
        ).upload_new(
            principal=principal(owner_id),
            space_id="chunk-space",
            display_name="Chunked notes",
            original_filename="notes.txt",
            mime_type="text/plain",
            chunks=_chunks(text.encode()),
            idempotency_key="chunk-e2e",
        )

        worker = DocumentIngestionWorker(
            factory,
            storage,
            _TextParser(),
            StubEmbeddingClient(),
            worker_id="chunk-e2e-worker",
            lease_seconds=30,
            heartbeat_interval_seconds=1,
            chunk_size=2000,
            chunk_overlap=200,
            max_chunks=10000,
        )
        registry = MetricsRegistry()
        token = metrics_registry_context.set(registry)
        try:
            assert await worker.run_once() == receipt.job_id
        finally:
            metrics_registry_context.reset(token)

        async with factory() as session:
            rows = list(
                await session.scalars(
                    sa_select(DocumentRevisionChunkModel).where(
                        DocumentRevisionChunkModel.document_revision_id == receipt.revision_id
                    )
                )
            )
            assert [row.content for row in rows] == [text[0:64], text[56:120], text[112:176], text[168:200]]
