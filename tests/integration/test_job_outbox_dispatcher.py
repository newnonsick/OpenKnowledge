import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import select, update

from src.gateway.application.services.document_upload_service import DocumentUploadService
from src.gateway.application.services.job_outbox_dispatcher import JobOutboxDispatcher, JobOutboxService
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.ingestion_models import JobOutboxModel
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.models import Workspace
from src.gateway.infrastructure.storage.versioned_local_storage import LocalVersionedObjectStorage
from tests.e2e.harness.test_env import TestEnvironment
from tests.integration.postgres_test_database import isolated_postgres_database


async def _chunks(value: bytes):
    yield value


def _principal() -> Principal:
    import hashlib

    return Principal(
        subject_id=str(UUID(bytes=hashlib.sha256(b"sk-test-admin").digest()[:16])),
        kind=PrincipalKind.COMPATIBILITY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:write"}),
    )


async def _queued_event(env, tmp_path):
    await DocumentUploadService(
        env.session_factory,
        LocalVersionedObjectStorage(tmp_path),
        max_upload_bytes=1024,
    ).upload_new(
        principal=_principal(),
        space_id="test_ws",
        display_name="Outbox",
        original_filename="outbox.txt",
        mime_type="text/plain",
        chunks=_chunks(b"outbox"),
        idempotency_key="outbox-upload",
    )
    async with env.session_factory() as session:
        return await session.scalar(select(JobOutboxModel))


async def test_outbox_dispatcher_publishes_with_stable_deduplication_key(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        event = await _queued_event(env, tmp_path)
        published = []

        async def publish(event_type, payload, deduplication_key):
            published.append((event_type, payload, deduplication_key))

        dispatcher = JobOutboxDispatcher(
            env.session_factory,
            publish,
            worker_id="outbox-a",
            lease_seconds=30,
        )

        assert await dispatcher.dispatch_once() == event.id
        assert published == [(event.event_type, event.payload, event.deduplication_key)]
        async with env.session_factory() as session:
            stored = await session.get(JobOutboxModel, event.id)
            assert stored.state == "published"
            assert stored.published_at is not None
            assert stored.attempt_count == 1
            assert stored.claim_token is None


async def test_outbox_dispatcher_retries_failure_and_reuses_same_key(tmp_path) -> None:
    async with TestEnvironment(storage_dir=tmp_path) as env:
        event = await _queued_event(env, tmp_path)
        keys = []

        async def fail_once(event_type, payload, deduplication_key):
            keys.append(deduplication_key)
            if len(keys) == 1:
                raise OSError("unavailable")

        dispatcher = JobOutboxDispatcher(
            env.session_factory,
            fail_once,
            worker_id="outbox-retry",
            lease_seconds=30,
        )

        assert await dispatcher.dispatch_once() == event.id
        async with env.session_factory.begin() as session:
            stored = await session.get(JobOutboxModel, event.id)
            assert stored.state == "pending"
            assert stored.last_error_code == "publish_oserror"
            await session.execute(
                update(JobOutboxModel)
                .where(JobOutboxModel.id == event.id)
                .values(available_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )

        assert await dispatcher.dispatch_once() == event.id
        assert keys == [event.deduplication_key, event.deduplication_key]
        async with env.session_factory() as session:
            stored = await session.get(JobOutboxModel, event.id)
            assert stored.state == "published"
            assert stored.attempt_count == 2


async def test_outbox_claim_is_exclusive_on_postgres(tmp_path) -> None:
    async with isolated_postgres_database() as (_, factory):
        member_id = uuid4()
        space_id = f"outbox-{uuid4().hex[:8]}"
        username = f"outbox-{uuid4().hex[:8]}"
        principal = Principal(
            subject_id=str(member_id),
            kind=PrincipalKind.SESSION,
            system_role=SystemRole.MEMBER,
            scopes=frozenset({"knowledge:write"}),
        )
        async with factory.begin() as session:
            session.add(
                MemberModel(
                    id=member_id,
                    username=username,
                    username_normalized=username,
                    display_name="Outbox PostgreSQL",
                    status="active",
                    system_role="member",
                    force_password_change=False,
                )
            )
            session.add(Workspace(id=space_id, name="Outbox PostgreSQL"))
            await session.flush()
            session.add(
                SpaceMembershipModel(
                    space_id=space_id,
                    member_id=member_id,
                    role="owner",
                )
            )
        await DocumentUploadService(
            factory,
            LocalVersionedObjectStorage(tmp_path),
            max_upload_bytes=1024,
        ).upload_new(
            principal=principal,
            space_id=space_id,
            display_name="Outbox PostgreSQL",
            original_filename="outbox-postgres.txt",
            mime_type="text/plain",
            chunks=_chunks(b"outbox postgres"),
            idempotency_key="outbox-postgres",
        )

        async def claim(worker_id):
            async with factory.begin() as session:
                return await JobOutboxService(session).claim_next(
                    worker_id,
                    lease_seconds=30,
                )

        claims = await asyncio.gather(claim("outbox-a"), claim("outbox-b"))
        claimed = [value for value in claims if value is not None]
        assert len(claimed) == 1

        async with factory.begin() as session:
            await JobOutboxService(session).complete(claimed[0])
