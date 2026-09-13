from datetime import datetime, timezone
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI

from src.gateway.application.services.session_service import SessionService
from src.gateway.application.services.webhook_service import (
    serialize_canonical,
    sign_webhook_payload,
    verify_webhook_signature,
    webhook_event_payload,
)
from src.gateway.config import Settings
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SpaceRole, SystemRole
from src.gateway.infrastructure.database import set_session_factory
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionModel, IngestionJobModel, JobOutboxModel
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


def test_webhook_signature_roundtrip() -> None:
    body = serialize_canonical({"id": "evt-1", "event_type": "ingestion.queued"})
    signature = sign_webhook_payload(body, secret="s3cret", timestamp="1700000000")
    assert verify_webhook_signature(signature, body, secret="s3cret", timestamp="1700000000") is True
    assert verify_webhook_signature(signature, body, secret="wrong", timestamp="1700000000") is False
    assert verify_webhook_signature(signature, body + b"x", secret="s3cret", timestamp="1700000000") is False
    assert verify_webhook_signature(signature, body, secret="s3cret", timestamp="1700000001") is False


def test_webhook_event_payload_shape() -> None:
    event = JobOutboxModel(
        job_id=uuid4(),
        event_type="ingestion.queued",
        deduplication_key="dedup-1",
        payload={"job_id": "j-1"},
    )
    body = webhook_event_payload(event)
    assert body["event_type"] == "ingestion.queued"
    assert body["deduplication_key"] == "dedup-1"
    assert body["payload"] == {"job_id": "j-1"}
    assert body["id"]
    canonical = serialize_canonical(body)
    assert verify_webhook_signature(
        sign_webhook_payload(canonical, secret="s", timestamp="1"),
        canonical,
        secret="s",
        timestamp="1",
    ) is True


async def test_events_endpoint_lists_with_cursor_and_filter(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    member_id = uuid4()
    job_id = uuid4()
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
            session.add(
                MemberModel(
                    id=member_id,
                    username="events-member",
                    username_normalized="events-member",
                    display_name="Events Member",
                    status=MemberStatus.ACTIVE.value,
                    system_role=SystemRole.MEMBER.value,
                    force_password_change=False,
                )
            )
            session.add(Workspace(id="global", name="Global", created_by_member_id=member_id))
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    id=uuid4(),
                    space_id="global",
                    member_id=member_id,
                    role=SpaceRole.EDITOR.value,
                )
            )
            first_id = uuid4()
            while True:
                second_id = uuid4()
                if second_id.int > first_id.int:
                    break
            document_id = uuid4()
            revision_id = uuid4()
            session.add(DocumentModel(id=document_id, space_id="global", display_name="webhook.txt", created_by_member_id=member_id))
            await session.flush()
            session.add(
                DocumentRevisionModel(
                    id=revision_id,
                    document_id=document_id,
                    space_id="global",
                    version=1,
                    original_filename="webhook.txt",
                    mime_type="text/plain",
                    size_bytes=4,
                    checksum_sha256="c" * 64,
                    staging_storage_key=f"staging/{revision_id}",
                    created_by_member_id=member_id,
                )
            )
            await session.flush()
            session.add(
                IngestionJobModel(
                    id=job_id,
                    space_id="global",
                    document_id=document_id,
                    document_revision_id=revision_id,
                    initiated_by_member_id=member_id,
                    idempotency_key="webhook-test-job",
                )
            )
            session.add(
                JobOutboxModel(
                    id=first_id,
                    job_id=job_id,
                    event_type="ingestion.queued",
                    deduplication_key="webhook-test-1",
                    payload={"job_id": str(job_id)},
                )
            )
            session.add(
                JobOutboxModel(
                    id=second_id,
                    job_id=job_id,
                    event_type="ingestion.succeeded",
                    deduplication_key="webhook-test-2",
                    payload={"job_id": str(job_id)},
                )
            )
            member_session = await SessionService(session).issue(
                _principal(member_id),
                now=now,
                step_up_at=now,
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
                listed = await client.get("/api/v1/events")
                assert listed.status_code == 200
                items = listed.json()["items"]
                by_id = {item["id"]: item for item in items}
                assert str(first_id) in by_id
                assert str(second_id) in by_id
                assert by_id[str(first_id)]["event_type"] == "ingestion.queued"
                assert by_id[str(first_id)]["deduplication_key"] == "webhook-test-1"

                filtered = await client.get("/api/v1/events", params={"event_type": "ingestion.succeeded"})
                assert filtered.status_code == 200
                assert [item["id"] for item in filtered.json()["items"]] == [str(second_id)]

                resumed = await client.get("/api/v1/events", params={"after_id": str(first_id)})
                assert resumed.status_code == 200
                assert [item["id"] for item in resumed.json()["items"]] == [str(second_id)]

                unknown = await client.get("/api/v1/events", params={"after_id": str(uuid4())})
                assert unknown.status_code == 404
                assert unknown.json()["error"]["type"] == "not_found_error"
        finally:
            set_session_factory(None)
