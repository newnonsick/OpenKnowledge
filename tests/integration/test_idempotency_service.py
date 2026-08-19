import asyncio
from datetime import datetime, timezone

import pytest

from src.gateway.application.services.idempotency_service import IdempotencyService, ReservationStatus
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_idempotency_replays_completed_result_and_rejects_changed_request() -> None:
    now = datetime(2026, 8, 20, 13, 0, tzinfo=timezone.utc)

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            service = IdempotencyService(session)
            reservation = await service.reserve(
                actor_id="member-1",
                operation="space.create",
                idempotency_key="retry-key",
                payload={"name": "Project", "nested": {"b": 2, "a": 1}},
                now=now,
            )
            assert reservation.status is ReservationStatus.RESERVED
            await service.complete(
                reservation.record_id,
                response_status=201,
                resource_ids=["space-1"],
            )

        async with factory.begin() as session:
            replay = await IdempotencyService(session).reserve(
                actor_id="member-1",
                operation="space.create",
                idempotency_key="retry-key",
                payload={"nested": {"a": 1, "b": 2}, "name": "Project"},
                now=now,
            )
            assert replay.status is ReservationStatus.REPLAY
            assert replay.response_status == 201
            assert replay.resource_ids == ("space-1",)
            await IdempotencyService(session).complete(
                replay.record_id,
                response_status=201,
                resource_ids=["space-1"],
            )
            with pytest.raises(Exception, match="already been completed"):
                await IdempotencyService(session).complete(
                    replay.record_id,
                    response_status=200,
                    resource_ids=["space-2"],
                )
            with pytest.raises(Exception, match="different request"):
                await IdempotencyService(session).reserve(
                    actor_id="member-1",
                    operation="space.create",
                    idempotency_key="retry-key",
                    payload={"name": "Different"},
                    now=now,
                )


async def test_concurrent_idempotency_reservations_have_one_owner() -> None:
    now = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)

    async with isolated_postgres_database() as (_, factory):
        first_reserved = asyncio.Event()
        release_first = asyncio.Event()

        async def reserve_first():
            async with factory.begin() as session:
                result = await IdempotencyService(session).reserve(
                    actor_id="member-2",
                    operation="api-key.create",
                    idempotency_key="concurrent-key",
                    payload={"name": "Laptop"},
                    now=now,
                )
                first_reserved.set()
                await release_first.wait()
                return result

        async def reserve_second():
            await first_reserved.wait()
            async with factory.begin() as session:
                return await IdempotencyService(session).reserve(
                    actor_id="member-2",
                    operation="api-key.create",
                    idempotency_key="concurrent-key",
                    payload={"name": "Laptop"},
                    now=now,
                )

        first_task = asyncio.create_task(reserve_first())
        second_task = asyncio.create_task(reserve_second())
        await first_reserved.wait()
        release_first.set()
        first, second = await asyncio.gather(first_task, second_task)
        assert first.status is ReservationStatus.RESERVED
        assert second.status is ReservationStatus.IN_PROGRESS
        assert first.record_id == second.record_id
