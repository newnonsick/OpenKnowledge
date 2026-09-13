from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select

import json as _json

from src.gateway.application.services.knowledge_export_service import (
    KnowledgeExportService,
    validate_export_document,
)
from src.gateway.application.services.session_service import SessionService
from src.gateway.application.use_cases.context import UseCaseContext
from src.gateway.config import Settings
from src.gateway.domain.exceptions import ValidationException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import KnowledgeItem as KnowledgeItemModel
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.presentation.auth import APIKeyAuthMiddleware
from src.gateway.presentation.errors import register_exception_handlers
from src.gateway.presentation.routers.management import router
from src.gateway.presentation.settings_context import SettingsContextMiddleware
from tests.integration.postgres_test_database import isolated_postgres_database


def _principal(member_id, system_role=SystemRole.MEMBER) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=system_role,
        scopes=frozenset({"*"}),
    )


def _settings(tmp_path) -> Settings:
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


def test_export_validation_rejects_bad_documents() -> None:
    with pytest.raises(ValidationException):
        validate_export_document({"format": "wrong"})
    with pytest.raises(ValidationException):
        validate_export_document(
            {
                "format": "openknowledge-knowledge-export",
                "version": 999,
                "space_id": "s",
                "items": [],
            }
        )
    with pytest.raises(ValidationException):
        validate_export_document(
            {
                "format": "openknowledge-knowledge-export",
                "version": 1,
                "space_id": "s",
                "items": [{"id": "not-a-uuid"}],
            }
        )


async def test_export_import_roundtrip_preserves_supplied_ids(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    member_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="export-member",
                    username_normalized="export-member",
                    display_name="Export Member",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="space-a", name="Space A", created_by_member_id=member_id))
            session.add(Workspace(id="space-b", name="Space B", created_by_member_id=member_id))
            await session.flush()
            session.add_all(
                [
                    SpaceMembershipModel(id=uuid4(), space_id="space-a", member_id=member_id, role="owner"),
                    SpaceMembershipModel(id=uuid4(), space_id="space-b", member_id=member_id, role="owner"),
                ]
            )
            member_session = await SessionService(session).issue(
                _principal(member_id), now=now, step_up_at=now
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
                cookies={"__Host-openknowledge-access": member_session.access_token.reveal()},
            ) as client:
                created = await client.post(
                    "/api/v1/knowledge",
                    json={"space_id": "space-a", "title": "Portable", "content": "Export me.", "tags": ["ops"]},
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": member_session.csrf_token.reveal(),
                        "Idempotency-Key": f"export-seed-{uuid4()}",
                    },
                )
                assert created.status_code == 201
                item_id = created.json()["id"]

                exported = await client.get("/api/v1/knowledge/export", params={"space_id": "space-a"})
                assert exported.status_code == 200
                document = exported.json()
                assert document["space_id"] == "space-a"
                assert len(document["items"]) == 1
                assert document["items"][0]["id"] == item_id
                assert document["items"][0]["revisions"][0]["content"] == "Export me."

                mismatch = await client.post(
                    "/api/v1/knowledge/import",
                    params={"space_id": "space-b"},
                    json=document,
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": member_session.csrf_token.reveal(),
                        "Idempotency-Key": f"export-mismatch-{uuid4()}",
                    },
                )
                assert mismatch.status_code == 422

                document["space_id"] = "space-b"
                for export_item in document["items"]:
                    export_item["id"] = str(uuid4())
                    for export_revision in export_item["revisions"]:
                        export_revision["id"] = str(uuid4())
                imported = await client.post(
                    "/api/v1/knowledge/import",
                    params={"space_id": "space-b"},
                    json=document,
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": member_session.csrf_token.reveal(),
                        "Idempotency-Key": f"export-import-{uuid4()}",
                    },
                )
                assert imported.status_code == 200
                assert imported.json() == {"space_id": "space-b", "created": 1, "skipped": 0}

                imported_item_id = document["items"][0]["id"]
                imported_revision_id = document["items"][0]["revisions"][0]["id"]
                refetched = await client.get(f"/api/v1/knowledge/{imported_item_id}")
                assert refetched.status_code == 200
                assert refetched.json()["content"] == "Export me."
                assert refetched.json()["space_id"] == "space-b"

                fetched_revision = await client.get(
                    f"/api/v1/evidence/knowledge/{imported_item_id}/revisions/{imported_revision_id}"
                )
                assert fetched_revision.status_code == 200
                assert fetched_revision.json()["revision_id"] == imported_revision_id

                replay_key = f"export-import-{uuid4()}"
                replayed_first = await client.post(
                    "/api/v1/knowledge/import",
                    params={"space_id": "space-b"},
                    json=document,
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": member_session.csrf_token.reveal(),
                        "Idempotency-Key": replay_key,
                    },
                )
                assert replayed_first.status_code == 200
                assert replayed_first.json() == {"space_id": "space-b", "created": 0, "skipped": 1}

                replayed = await client.post(
                    "/api/v1/knowledge/import",
                    params={"space_id": "space-b"},
                    json=document,
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": member_session.csrf_token.reveal(),
                        "Idempotency-Key": replay_key,
                    },
                )
                assert replayed.status_code == 200
                assert replayed.json() == {"space_id": "space-b", "created": 0, "skipped": 1}

                altered = _json.loads(_json.dumps(document))
                altered["items"][0]["title"] = "Changed title"
                conflict = await client.post(
                    "/api/v1/knowledge/import",
                    params={"space_id": "space-b"},
                    json=altered,
                    headers={
                        "Origin": "https://gateway.test",
                        "X-CSRF-Token": member_session.csrf_token.reveal(),
                        "Idempotency-Key": replay_key,
                    },
                )
                assert conflict.status_code == 409
        finally:
            set_session_factory(None)


async def test_export_import_service_layer_grant_boundary(tmp_path) -> None:
    member_id = uuid4()
    other_id = uuid4()
    settings = _settings(tmp_path)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username="export-owner",
                    username_normalized="export-owner",
                    display_name="Export Owner",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(
                MemberModel(
                    id=other_id,
                    username="export-stranger",
                    username_normalized="export-stranger",
                    display_name="Export Stranger",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="space-a", name="Space A", created_by_member_id=member_id))
            await session.flush()
            session.add(
                SpaceMembershipModel(id=uuid4(), space_id="space-a", member_id=member_id, role="owner")
            )
        async with factory.begin() as session:
            from src.gateway.domain.exceptions import AuthorizationException

            with pytest.raises(AuthorizationException):
                await KnowledgeExportService(session).export_space(
                    UseCaseContext(principal=_principal(other_id)), "space-a"
                )
            with pytest.raises(AuthorizationException):
                await KnowledgeExportService(session).import_space(
                    UseCaseContext(principal=_principal(other_id)),
                    "space-a",
                    {
                        "format": "openknowledge-knowledge-export",
                        "version": 1,
                        "space_id": "space-a",
                        "items": [],
                    },
                )
        async with factory() as session:
            rows = list(
                await session.scalars(
                    select(KnowledgeItemModel).where(KnowledgeItemModel.workspace_id == "space-a")
                )
            )
            assert rows == []
