import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from src.gateway.application.services.ingestion_job_service import IngestionJobService
from src.gateway.domain.exceptions import ConcurrencyConflictException, JobLeaseLostException
from src.gateway.infrastructure.persistence.identity_models import MemberModel
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionModel, IngestionJobModel
from src.gateway.infrastructure.persistence.models import Workspace
from tests.integration.postgres_test_database import isolated_postgres_database


async def _seed_job(factory, *, suffix: str, idempotency_key: str):
    member_id = uuid4()
    document_id = uuid4()
    revision_id = uuid4()
    job_id = uuid4()
    space_id = f"jobs-{suffix}"
    async with factory.begin() as session:
        session.add(
            MemberModel(
                id=member_id,
                username=f"member-{suffix}",
                username_normalized=f"member-{suffix}",
                display_name=f"Member {suffix}",
                status="active",
                system_role="member",
                force_password_change=False,
            )
        )
        session.add(Workspace(id=space_id, name=f"Jobs {suffix}"))
        await session.flush()
        session.add(
            DocumentModel(
                id=document_id,
                space_id=space_id,
                display_name=f"{suffix}.txt",
                created_by_member_id=member_id,
            )
        )
        await session.flush()
        session.add(
            DocumentRevisionModel(
                id=revision_id,
                document_id=document_id,
                space_id=space_id,
                version=1,
                original_filename=f"{suffix}.txt",
                mime_type="text/plain",
                size_bytes=4,
                checksum_sha256="a" * 64,
                staging_storage_key=f"staging/{revision_id}",
                created_by_member_id=member_id,
            )
        )
        await session.flush()
        session.add(
            IngestionJobModel(
                id=job_id,
                space_id=space_id,
                document_id=document_id,
                document_revision_id=revision_id,
                initiated_by_member_id=member_id,
                idempotency_key=idempotency_key,
            )
        )
    return job_id


async def test_job_claim_is_exclusive_and_terminal_updates_are_fenced() -> None:
    async with isolated_postgres_database() as (_, factory):
        job_id = await _seed_job(factory, suffix=uuid4().hex[:8], idempotency_key="claim-once")

        async with factory.begin() as session:
            claim = await IngestionJobService(session).claim_next("worker-a", lease_seconds=30)
        assert claim is not None
        assert claim.job_id == job_id

        async with factory.begin() as session:
            assert await IngestionJobService(session).claim_next("worker-b", lease_seconds=30) is None

        async with factory.begin() as session:
            with pytest.raises(JobLeaseLostException):
                await IngestionJobService(session).complete(job_id, uuid4())

        async with factory.begin() as session:
            await IngestionJobService(session).complete(job_id, claim.claim_token)

        async with factory() as session:
            job = await session.get(IngestionJobModel, job_id)
            assert job.state == "succeeded"
            assert job.progress == 100
            assert job.finished_at is not None
            assert job.claim_token is None


async def test_expired_claim_is_recovered_and_stale_worker_cannot_commit() -> None:
    async with isolated_postgres_database() as (_, factory):
        job_id = await _seed_job(factory, suffix=uuid4().hex[:8], idempotency_key="recover")

        async with factory.begin() as session:
            first = await IngestionJobService(session).claim_next("worker-a", lease_seconds=30)
        async with factory.begin() as session:
            await session.execute(
                update(IngestionJobModel)
                .where(IngestionJobModel.id == job_id)
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=60))
            )
        async with factory.begin() as session:
            with pytest.raises(JobLeaseLostException):
                await IngestionJobService(session).complete(job_id, first.claim_token)
        async with factory.begin() as session:
            second = await IngestionJobService(session).claim_next("worker-b", lease_seconds=30)

        assert first is not None
        assert second is not None
        assert first.claim_token != second.claim_token

        async with factory.begin() as session:
            with pytest.raises(JobLeaseLostException):
                await IngestionJobService(session).complete(job_id, first.claim_token)

        async with factory.begin() as session:
            await IngestionJobService(session).complete(job_id, second.claim_token)


async def test_expired_final_attempt_is_failed_instead_of_becoming_stuck() -> None:
    async with isolated_postgres_database() as (_, factory):
        job_id = await _seed_job(factory, suffix=uuid4().hex[:8], idempotency_key="exhausted")

        async with factory.begin() as session:
            job = await session.get(IngestionJobModel, job_id)
            job.max_attempts = 1

        async with factory.begin() as session:
            claim = await IngestionJobService(session).claim_next("worker-a", lease_seconds=30)
        assert claim is not None

        async with factory.begin() as session:
            await session.execute(
                update(IngestionJobModel)
                .where(IngestionJobModel.id == job_id)
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=60))
            )

        async with factory.begin() as session:
            assert await IngestionJobService(session).claim_next("worker-b", lease_seconds=30) is None

        async with factory() as session:
            job = await session.get(IngestionJobModel, job_id)
            revision = await session.get(DocumentRevisionModel, job.document_revision_id)
            assert job.state == "failed"
            assert job.last_error_code == "attempts_exhausted"
            assert job.finished_at is not None
            assert job.claim_token is None
            assert revision.status == "failed"
            assert revision.failure_code == "attempts_exhausted"


async def test_concurrent_workers_claim_distinct_jobs_and_cancellation_is_cooperative() -> None:
    async with isolated_postgres_database() as (_, factory):
        first_id = await _seed_job(factory, suffix=uuid4().hex[:8], idempotency_key="parallel-a")
        second_id = await _seed_job(factory, suffix=uuid4().hex[:8], idempotency_key="parallel-b")

        async def claim(worker_id: str):
            async with factory.begin() as session:
                return await IngestionJobService(session).claim_next(worker_id, lease_seconds=30)

        first_claim, second_claim = await asyncio.gather(claim("worker-a"), claim("worker-b"))
        assert first_claim is not None
        assert second_claim is not None
        assert {first_claim.job_id, second_claim.job_id} == {first_id, second_id}

        async with factory.begin() as session:
            await IngestionJobService(session).request_cancellation(first_claim.job_id)
        async with factory.begin() as session:
            assert await IngestionJobService(session).is_cancellation_requested(
                first_claim.job_id,
                first_claim.claim_token,
            )
            await IngestionJobService(session).acknowledge_cancellation(
                first_claim.job_id,
                first_claim.claim_token,
            )

        async with factory() as session:
            states = dict(
                (
                    await session.execute(
                        select(IngestionJobModel.id, IngestionJobModel.state).where(
                            IngestionJobModel.id.in_([first_id, second_id])
                        )
                    )
                ).all()
            )
            assert states[first_claim.job_id] == "cancelled"
            assert states[second_claim.job_id] == "running"


async def test_queued_cancellation_marks_pending_revision_cancelled() -> None:
    async with isolated_postgres_database() as (_, factory):
        job_id = await _seed_job(factory, suffix=uuid4().hex[:8], idempotency_key="cancel-queued")

        async with factory.begin() as session:
            assert await IngestionJobService(session).request_cancellation(job_id) == "cancellation_requested"
        async with factory.begin() as session:
            assert await IngestionJobService(session).claim_next("cancellation-sweeper", lease_seconds=30) is None

        async with factory() as session:
            job = await session.get(IngestionJobModel, job_id)
            revision = await session.get(DocumentRevisionModel, job.document_revision_id)
            assert job.state == "cancelled"
            assert revision.status == "cancelled"
            assert revision.failure_code == "cancelled"


async def test_expired_cancelled_claim_is_finalized_without_reclaim() -> None:
    async with isolated_postgres_database() as (_, factory):
        job_id = await _seed_job(factory, suffix=uuid4().hex[:8], idempotency_key="cancel-expired")
        async with factory.begin() as session:
            claim = await IngestionJobService(session).claim_next("worker-a", lease_seconds=30)
        async with factory.begin() as session:
            await IngestionJobService(session).request_cancellation(job_id)
            await session.execute(
                update(IngestionJobModel)
                .where(IngestionJobModel.id == job_id)
                .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=60))
            )
        async with factory.begin() as session:
            assert await IngestionJobService(session).claim_next("worker-b", lease_seconds=30) is None

        async with factory() as session:
            job = await session.get(IngestionJobModel, job_id)
            revision = await session.get(DocumentRevisionModel, job.document_revision_id)
            assert claim is not None
            assert job.state == "cancelled"
            assert job.claim_token is None
            assert revision.status == "cancelled"


async def test_explicit_retry_resets_terminal_job_but_rejects_quarantine() -> None:
    async with isolated_postgres_database() as (_, factory):
        job_id = await _seed_job(factory, suffix=uuid4().hex[:8], idempotency_key="explicit-retry")
        async with factory.begin() as session:
            job = await session.get(IngestionJobModel, job_id)
            revision_id = job.document_revision_id
            job.state = "failed"
            job.attempt_count = job.max_attempts
            job.finished_at = datetime.now(timezone.utc)
            job.last_error_code = "embedding_provider_error"
            revision = await session.get(DocumentRevisionModel, revision_id)
            revision.status = "failed"
            revision.failure_code = "embedding_provider_error"
            revision.storage_key = f"objects/{revision.space_id}/{revision.document_id}/{revision.id}"
            revision.staging_storage_key = None

        async with factory.begin() as session:
            assert await IngestionJobService(session).request_retry(job_id) == "retry_requested"

        async with factory() as session:
            job = await session.get(IngestionJobModel, job_id)
            revision = await session.get(DocumentRevisionModel, revision_id)
            assert job.state == "failed"
            assert job.retry_requested
            assert revision.status == "failed"

        async with factory.begin() as session:
            claim = await IngestionJobService(session).claim_next("retry-worker", lease_seconds=30)
        assert claim.job_id == job_id

        async with factory() as session:
            job = await session.get(IngestionJobModel, job_id)
            revision = await session.get(DocumentRevisionModel, revision_id)
            assert job.state == "running"
            assert job.attempt_count == 1
            assert not job.retry_requested
            assert job.last_error_code is None
            assert revision.status == "pending"
            assert revision.failure_code is None

        async with factory.begin() as session:
            await IngestionJobService(session).fail(
                job_id,
                claim.claim_token,
                error_code="terminal",
                retryable=False,
            )
            revision = await session.get(DocumentRevisionModel, revision_id)
            revision.status = "quarantined"
            revision.failure_code = "storage_checksum_mismatch"

        async with factory.begin() as session:
            with pytest.raises(ConcurrencyConflictException):
                await IngestionJobService(session).request_retry(job_id)
