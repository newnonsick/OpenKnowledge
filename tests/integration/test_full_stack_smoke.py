from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pyotp
from cryptography.fernet import Fernet
from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from src.gateway.application.security.passwords import PasswordService
from src.gateway.application.services.bootstrap_service import BootstrapService
from src.gateway.application.services.document_ingestion_worker import DocumentIngestionWorker
from src.gateway.application.services.bounded_document_parser import ParsedDocument
from src.gateway.config import Settings, get_settings
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, IngestionJobModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM as _IMPORT_TIME_EMBED_DIM
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from src.gateway.observability import metrics_registry_context
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.metrics import MetricsRegistry
from src.gateway.presentation.routers.management import router as management_router
from src.gateway.presentation.routers.management_auth import router as management_auth_router
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


class _Parser:
    async def parse(self, *, filename: str, mime_type: str, content: bytes) -> ParsedDocument:
        return ParsedDocument(text=content.decode(), parser_version="smoke-test-v1")


def _smoke_embedding_dimension() -> int:
    return get_settings().embedding.dimension


class _EmbeddingClient:
    dimension = _IMPORT_TIME_EMBED_DIM

    def _size(self) -> int:
        return _smoke_embedding_dimension()

    async def embed_texts(self, texts):
        size = self._size()
        return [[1.0] + [0.0] * (size - 1) for _ in texts]

    async def embed_query(self, query):
        size = self._size()
        return [1.0] + [0.0] * (size - 1)


async def test_full_stack_smoke_login_rotate_upload_ingest_search_revoke(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    mfa_key = Fernet.generate_key().decode("ascii")
    settings = Settings(
        gateway={
            "environment": "test",
            "public_base_url": "https://gateway.test",
            "trusted_hosts": ["gateway.test"],
            "api_key_peppers": {1: "test-api-key-pepper-with-adequate-length"},
            "active_api_key_pepper_version": 1,
            "mfa_encryption_keys": {1: mfa_key},
            "active_mfa_encryption_key_version": 1,
            "storage_dir": str(tmp_path / "storage"),
        }
    )
    password_service = PasswordService(memory_cost=8192, time_cost=2, parallelism=1)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            bootstrap = await BootstrapService(session, password_service).create_first_super_admin(
                username="admin",
                display_name="Admin",
                request_id="smoke-bootstrap",
                now=now,
            )
            temporary_password = bootstrap.temporary_password.reveal()
            session.add(
                EmbeddingGenerationModel(
                    id=uuid4(),
                    purpose="retrieval",
                    model_id="smoke-test-generation",
                    dimensions=_smoke_embedding_dimension(),
                    status="active",
                )
            )

        storage = LocalVersionedObjectStorage(tmp_path / "storage")
        worker = DocumentIngestionWorker(
            factory,
            storage,
            _Parser(),
            _EmbeddingClient(),
            worker_id="smoke-worker",
            lease_seconds=30,
            heartbeat_interval_seconds=1,
            chunk_size=8,
            chunk_overlap=2,
            max_chunks=20,
        )
        registry = MetricsRegistry()
        metrics_token = metrics_registry_context.set(registry)
        try:
            import src.gateway.application.services.authorized_retrieval_service as retrieval_module
            import src.gateway.application.services.knowledge_management_service as knowledge_module

            original_retrieval_client = retrieval_module.default_retrieval_embedding_client
            original_knowledge_client = knowledge_module.default_embedding_client
            retrieval_module.default_retrieval_embedding_client = lambda: _EmbeddingClient()
            knowledge_module.default_embedding_client = lambda: _EmbeddingClient()

            app = FastAPI()
            app.state.settings = settings
            register_exception_handlers(app)
            app.add_middleware(
                APIKeyAuthMiddleware,
                allowed_keys=[],
                api_key_peppers={1: "test-api-key-pepper-with-adequate-length"},
                session_factory=factory,
            )
            app.add_middleware(SettingsContextMiddleware)
            app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.gateway.trusted_hosts)
            app.include_router(management_auth_router)
            app.include_router(management_router)
            set_session_factory(factory)
            try:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="https://gateway.test") as client:
                    rejected = await client.post(
                        "/api/v1/auth/login",
                        headers={"Host": "evil.example"},
                        json={"username": "admin", "password": temporary_password},
                    )
                    assert rejected.status_code == 400
                    first_login = await client.post(
                        "/api/v1/auth/login",
                        json={"username": "admin", "password": temporary_password},
                    )
                    assert first_login.status_code == 200
                    assert first_login.json()["requires_password_change"] is True
                    csrf = client.cookies.get("openknowledge-csrf")
                    assert csrf

                    password_change = await client.post(
                        "/api/v1/auth/password",
                        headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                        json={
                            "password": "Permanent-Password-934!",
                            "confirmation": "Permanent-Password-934!",
                        },
                    )
                    assert password_change.status_code == 200

                    csrf = client.cookies.get("openknowledge-csrf")
                    enrollment = await client.post(
                        "/api/v1/auth/mfa/totp/enroll",
                        headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                        json={},
                    )
                    assert enrollment.status_code == 200
                    secret = enrollment.json()["secret"]
                    factor_id = enrollment.json()["factor_id"]

                    import asyncio as _asyncio
                    import time as _time

                    _last_totp_step = {"step": int(_time.time()) // 30}

                    async def _next_window_totp_code():
                        while True:
                            step = int(_time.time()) // 30
                            if step != _last_totp_step["step"]:
                                _last_totp_step["step"] = step
                                return pyotp.TOTP(secret).at(step * 30)
                            await _asyncio.sleep(1)

                    csrf = client.cookies.get("openknowledge-csrf")
                    confirmation = await client.post(
                        "/api/v1/auth/mfa/totp/confirm",
                        headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                        json={"factor_id": factor_id, "code": await _next_window_totp_code()},
                    )
                    assert confirmation.status_code == 200
                    api_secret = confirmation.json()["initial_api_key"]["secret"]
                    key_id = confirmation.json()["initial_api_key"]["id"]

                    old_refresh = client.cookies.get("__Secure-openknowledge-refresh")
                    csrf = client.cookies.get("openknowledge-csrf")
                    refreshed = await client.post(
                        "/api/v1/auth/refresh",
                        headers={"Origin": "https://gateway.test", "X-CSRF-Token": csrf},
                        json={},
                    )
                    assert refreshed.status_code == 200
                    assert client.cookies.get("__Secure-openknowledge-refresh") != old_refresh

                    client.headers["Authorization"] = f"Bearer {api_secret}"
                    knowledge = await client.post(
                        "/api/v1/knowledge",
                        headers={"Idempotency-Key": "smoke-note"},
                        json={
                            "space_id": "global",
                            "title": "Smoke note",
                            "content": "The blue valve closes clockwise.",
                            "tags": ["smoke"],
                        },
                    )
                    assert knowledge.status_code == 201
                    item_id = knowledge.json()["id"]

                    fetched = await client.get(f"/api/v1/knowledge/{item_id}")
                    assert fetched.status_code == 200
                    assert fetched.json()["title"] == "Smoke note"

                    upload = await client.post(
                        "/api/v1/sources/upload",
                        headers={"Idempotency-Key": "smoke-upload"},
                        files={"file": ("smoke.txt", b"The blue valve closes clockwise.", "text/plain")},
                        data={"space_id": "global", "display_name": "Smoke source"},
                    )
                    assert upload.status_code == 202
                    job_id = upload.json()["job_id"]

                    processed = await worker.run_once()
                    assert str(processed) == job_id

                    async with factory() as verification:
                        job = await verification.get(IngestionJobModel, job_id)
                        assert job.state == "succeeded"

                    search = await client.post(
                        "/api/v1/retrieval/search",
                        headers={"Idempotency-Key": "smoke-search"},
                        json={"query": "blue valve", "limit": 10},
                    )
                    assert search.status_code == 200
                    assert search.json()["hits"]

                    del client.headers["Authorization"]
                    session_login = await client.post(
                        "/api/v1/auth/login",
                        json={
                            "username": "admin",
                            "password": "Permanent-Password-934!",
                            "totp_code": await _next_window_totp_code(),
                        },
                    )
                    assert session_login.status_code == 200
                    session_csrf = client.cookies.get("openknowledge-csrf")
                    revoke = await client.delete(
                        f"/api/v1/api-keys/{key_id}",
                        headers={
                            "Origin": "https://gateway.test",
                            "X-CSRF-Token": session_csrf,
                            "Idempotency-Key": "smoke-revoke",
                        },
                    )
                    assert revoke.status_code == 204

                    client.headers["Authorization"] = f"Bearer {api_secret}"
                    denied = await client.post(
                        "/api/v1/retrieval/search",
                        headers={"Idempotency-Key": "smoke-search-denied"},
                        json={"query": "blue valve", "limit": 10},
                    )
                    assert denied.status_code in (401, 403, 404)
            finally:
                set_session_factory(None)
                retrieval_module.default_retrieval_embedding_client = original_retrieval_client
                knowledge_module.default_embedding_client = original_knowledge_client
            assert 'gateway_ingestion_events_total{event="terminal",outcome="succeeded"} 1' in registry.render()
        finally:
            metrics_registry_context.reset(metrics_token)
