from datetime import datetime, timezone
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from src.gateway.application.services.knowledge_management_service import (
    KnowledgeManagementService as KnowledgeService,
)
import src.gateway.application.services.knowledge_management_service as knowledge_module
from src.gateway.application.services.session_service import SessionService
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, Workspace
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
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


async def test_evidence_resolves_exact_revision_with_permission_recheck(tmp_path, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    member_id = uuid4()
    outsider_id = uuid4()
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
                        id=member_id,
                        username="member",
                        username_normalized="member",
                        display_name="Member",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=outsider_id,
                        username="outsider",
                        username_normalized="outsider",
                        display_name="Outsider",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                    Workspace(id="global", name="Family Shared", created_by_member_id=member_id),
                    EmbeddingGenerationModel(
                        id=uuid4(),
                        purpose="retrieval",
                        model_id="offline-test-generation",
                        dimensions=EMBED_DIM,
                        status="active",
                        activated_at=now,
                    ),
                ]
            )
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    id=uuid4(),
                    space_id="global",
                    member_id=member_id,
                    role=SpaceRole.EDITOR.value,
                )
            )
            member_session = await SessionService(session).issue(
                principal(member_id), now=now, step_up_at=now
            )
            outsider_session = await SessionService(session).issue(
                principal(outsider_id), now=now, step_up_at=now
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
        monkeypatch.setattr(knowledge_module, "default_embedding_client", lambda: StubEmbeddingClient())
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": member_session.access_token.reveal()},
            ) as member_client:
                member_headers = {
                    "Origin": "https://gateway.test",
                    "X-CSRF-Token": member_session.csrf_token.reveal(),
                }
                created = await member_client.post(
                    "/api/v1/knowledge",
                    headers={**member_headers, "Idempotency-Key": "evidence-note-v1"},
                    json={"space_id": "global", "title": "Valve", "content": "version one", "tags": []},
                )
                assert created.status_code == 201
                item_id = created.json()["id"]

                search_v1 = await member_client.post(
                    "/api/v1/retrieval/search",
                    headers={**member_headers, "Idempotency-Key": "evidence-search-v1"},
                    json={"query": "version one", "limit": 10},
                )
                assert search_v1.status_code == 200
                first_revision = next(
                    hit["revision_id"]
                    for hit in search_v1.json()["hits"]
                    if hit["canonical_id"] == item_id
                )

                updated = await member_client.put(
                    f"/api/v1/knowledge/{item_id}",
                    headers={**member_headers, "Idempotency-Key": "evidence-note-v2"},
                    json={"expected_version": 1, "title": "Valve", "content": "version two", "tags": []},
                )
                assert updated.status_code == 200

                old_citation = f"openknowledge://spaces/global/knowledge/{item_id}/revisions/{first_revision}"
                resolved = await member_client.post(
                    "/api/v1/evidence/resolve",
                    headers=member_headers,
                    json={"citation_uri": old_citation},
                )
                assert resolved.status_code == 200
                body = resolved.json()
                assert body["content"] == "version one"
                assert body["revision_id"] == first_revision
                assert body["superseded"] is True
                assert body["citation_uri"] == old_citation

                fetched = await member_client.get(
                    f"/api/v1/evidence/knowledge/{item_id}/revisions/{first_revision}",
                    headers=member_headers,
                )
                assert fetched.status_code == 200
                assert fetched.json()["content"] == "version one"

                bad_shape = await member_client.post(
                    "/api/v1/evidence/resolve",
                    headers=member_headers,
                    json={"citation_uri": "https://example.com/nope"},
                )
                assert bad_shape.status_code == 422

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": outsider_session.access_token.reveal()},
            ) as outsider_client:
                outsider_headers = {
                    "Origin": "https://gateway.test",
                    "X-CSRF-Token": outsider_session.csrf_token.reveal(),
                }
                denied = await outsider_client.post(
                    "/api/v1/evidence/resolve",
                    headers=outsider_headers,
                    json={"citation_uri": old_citation},
                )
                assert denied.status_code in (401, 403, 404)

            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://gateway.test",
                cookies={"__Host-openknowledge-access": member_session.access_token.reveal()},
            ) as member_client:
                member_headers = {
                    "Origin": "https://gateway.test",
                    "X-CSRF-Token": member_session.csrf_token.reveal(),
                }
                deleted = await member_client.delete(
                    f"/api/v1/knowledge/{item_id}?expected_version=2",
                    headers={**member_headers, "Idempotency-Key": "evidence-delete"},
                )
                assert deleted.status_code == 204
                gone = await member_client.post(
                    "/api/v1/evidence/resolve",
                    headers=member_headers,
                    json={"citation_uri": old_citation},
                )
                assert gone.status_code == 409
        finally:
            set_session_factory(None)
