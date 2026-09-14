from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import JSON, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.gateway.application.services.embedding_lifecycle_service import (
    EmbeddingGenerationLifecycleService,
)
from src.gateway.application.services.shadow_evaluation_service import (
    ShadowEvalScores,
    ShadowEvalThresholds,
    assert_single_generation,
    compare_shadow_scores,
    load_shadow_eval_slice,
)
from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.ingestion_models import (
    EmbeddingGenerationModel,
    MigrationBackfillRunModel,
)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):
    return compiler.visit_JSON(JSON(), **kw)


def _principal(member_id: UUID | None = None) -> Principal:
    return Principal(
        subject_id=str(member_id or uuid4()),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read"}),
    )


def _scores(recall: float, no_answer: float = 1.0, coverage: float = 1.0) -> ShadowEvalScores:
    total = 20
    recalled = round(recall * total)
    return ShadowEvalScores(
        lexical_recall=recall,
        recalled=recalled,
        total=total,
        missed=tuple(),
        no_answer_precision=no_answer,
        no_answer_quiet=3 if no_answer >= 1.0 else 0,
        no_answer_total=3,
        coverage=coverage,
    )


async def _lifecycle(factory: async_sessionmaker, evaluator=None):
    session = factory()
    service = EmbeddingGenerationLifecycleService(session, shadow_evaluator=evaluator)
    return session, service


async def _seed_generations(session, *, current_model="model-a", shadow_model="model-b"):
    now = datetime.now(timezone.utc)
    current = EmbeddingGenerationModel(
        purpose="retrieval", model_id=current_model, dimensions=8, status="active", activated_at=now
    )
    shadow = EmbeddingGenerationModel(
        purpose="retrieval", model_id=shadow_model, dimensions=8, status="building"
    )
    session.add_all([current, shadow])
    await session.flush()
    await session.refresh(current)
    await session.refresh(shadow)
    return current, shadow


@pytest.fixture()
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(
            EmbeddingGenerationModel.metadata.create_all,
            tables=[EmbeddingGenerationModel.__table__, MigrationBackfillRunModel.__table__],
        )
        await connection.execute(
            EmbeddingGenerationModel.__table__.delete().where(
                EmbeddingGenerationModel.__table__.c.purpose == "__shadow_eval_probe__"
            )
        )
        for index in ("uq_embedding_generations_active_purpose",):
            try:
                await connection.execute(text(f"DROP INDEX IF EXISTS {index}"))
            except Exception:
                pass
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


def test_shadow_eval_slice_matches_v2_fixture_contract() -> None:
    evaluation_slice = load_shadow_eval_slice()
    assert len(evaluation_slice.recall_cases) == 20
    assert len(evaluation_slice.no_answer_cases) == 3
    assert evaluation_slice.minimum_recall == 1.0
    assert evaluation_slice.spaces == tuple(f"slice-{index}" for index in range(6))


def test_compare_shadow_scores_allows_parity() -> None:
    comparison = compare_shadow_scores(_scores(1.0), _scores(1.0))
    assert comparison.allowed
    assert comparison.block_reasons == ()
    assert comparison.recall_regression == 0.0


def test_compare_shadow_scores_blocks_recall_regression() -> None:
    comparison = compare_shadow_scores(_scores(1.0), _scores(0.9))
    assert not comparison.allowed
    assert "recall_regression" in comparison.block_reasons
    assert comparison.recall_regression == pytest.approx(0.1)


def test_compare_shadow_scores_blocks_low_coverage_and_no_answer() -> None:
    comparison = compare_shadow_scores(_scores(1.0), _scores(1.0, no_answer=0.5, coverage=0.5))
    assert not comparison.allowed
    assert "coverage" in comparison.block_reasons
    assert "no_answer_precision" in comparison.block_reasons


def test_compare_shadow_scores_honors_custom_thresholds() -> None:
    thresholds = ShadowEvalThresholds(max_recall_regression=0.2, min_coverage=0.5)
    comparison = compare_shadow_scores(
        _scores(1.0), _scores(0.9, coverage=0.6), thresholds=thresholds
    )
    assert comparison.allowed


def test_assert_single_generation_rejects_mixed_generations() -> None:
    first, second = uuid4(), uuid4()
    assert assert_single_generation([first, first]) == first
    assert assert_single_generation([]) is None
    with pytest.raises(ResourceConflictException):
        assert_single_generation([first, second])


@dataclass
class _StubEvaluator:
    report: object = None
    error: BaseException | None = None
    calls: list = None

    def __post_init__(self) -> None:
        self.calls = []

    async def evaluate(self, principal, generation_id, *, thresholds=None):
        self.calls.append(generation_id)
        if self.error is not None:
            raise self.error
        return self.report


def _report(shadow_id: UUID, *, allowed: bool, reasons: tuple[str, ...] = (), current_id: UUID | None = None):
    from src.gateway.application.services.shadow_evaluation_service import ShadowEvalReport

    return ShadowEvalReport(
        current_generation_id=current_id or uuid4(),
        shadow_generation_id=shadow_id,
        current=_scores(1.0),
        shadow=_scores(1.0 if allowed else 0.5),
        recall_regression=0.0 if allowed else 0.5,
        allowed=allowed,
        block_reasons=reasons if reasons else (() if allowed else ("recall_regression",)),
    )


async def test_begin_shadow_generation_rejects_duplicate_config(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        current, _ = await _seed_generations(session)
        with pytest.raises(ResourceConflictException):
            await service.begin_shadow_generation(model_id=current.model_id, dimensions=8)
    finally:
        await session.close()


async def test_begin_shadow_generation_creates_building_generation(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        await _seed_generations(session)
        shadow = await service.begin_shadow_generation(model_id="model-c", dimensions=8)
        assert shadow.status == "building"
        stored = await session.get(EmbeddingGenerationModel, shadow.id)
        assert stored is not None and stored.status == "building"
    finally:
        await session.close()


async def test_promote_blocked_on_shadow_regression(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        _, shadow = await _seed_generations(session)
        evaluator = _StubEvaluator(report=_report(shadow.id, allowed=False))
        service = EmbeddingGenerationLifecycleService(session, shadow_evaluator=evaluator)
        with pytest.raises(ResourceConflictException) as excinfo:
            await service.promote(shadow.id, force=False, principal=_principal())
        assert "recall_regression" in excinfo.value.details["block_reasons"]
        stored = await session.get(EmbeddingGenerationModel, shadow.id)
        assert stored.status == "building"
    finally:
        await session.close()


async def test_promote_allowed_on_shadow_pass(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        current, shadow = await _seed_generations(session)
        evaluator = _StubEvaluator(report=_report(shadow.id, allowed=True))
        service = EmbeddingGenerationLifecycleService(session, shadow_evaluator=evaluator)
        outcome = await service.promote(shadow.id, force=False, principal=_principal())
        assert outcome.promoted.id == shadow.id
        assert outcome.retired_id == current.id
        assert evaluator.calls == [shadow.id]
    finally:
        await session.close()


async def test_promote_without_principal_or_report_is_blocked(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        _, shadow = await _seed_generations(session)
        with pytest.raises(ResourceConflictException):
            await service.promote(shadow.id, force=False)
    finally:
        await session.close()


async def test_promote_accepts_precomputed_passing_report(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        current, shadow = await _seed_generations(session)
        evaluator = _StubEvaluator(report=_report(uuid4(), allowed=True))
        service = EmbeddingGenerationLifecycleService(session, shadow_evaluator=evaluator)
        outcome = await service.promote(
            shadow.id,
            force=False,
            shadow_report=_report(shadow.id, allowed=True, current_id=current.id),
        )
        assert outcome.promoted.id == shadow.id
        assert evaluator.calls == []
    finally:
        await session.close()


async def test_promote_stale_precomputed_report_triggers_fresh_eval(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        _, shadow = await _seed_generations(session)
        evaluator = _StubEvaluator(report=_report(shadow.id, allowed=True))
        service = EmbeddingGenerationLifecycleService(session, shadow_evaluator=evaluator)
        outcome = await service.promote(
            shadow.id,
            force=False,
            principal=_principal(),
            shadow_report=_report(uuid4(), allowed=True),
        )
        assert outcome.promoted.id == shadow.id
        assert evaluator.calls == [shadow.id]
    finally:
        await session.close()


async def test_promote_force_bypasses_shadow_gate(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        _, shadow = await _seed_generations(session)
        evaluator = _StubEvaluator(report=_report(shadow.id, allowed=False))
        service = EmbeddingGenerationLifecycleService(session, shadow_evaluator=evaluator)
        outcome = await service.promote(shadow.id, force=True)
        assert outcome.promoted.id == shadow.id
        assert evaluator.calls == []
    finally:
        await session.close()


async def test_evaluate_shadow_rejects_non_building_generation(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        current, _ = await _seed_generations(session)
        with pytest.raises(AuthorizationException):
            await service.evaluate_shadow(_principal(), current.id)
    finally:
        await session.close()


async def test_rollback_restores_previous_active_generation(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        current, shadow = await _seed_generations(session)
        evaluator = _StubEvaluator(report=_report(shadow.id, allowed=True))
        service = EmbeddingGenerationLifecycleService(session, shadow_evaluator=evaluator)
        await service.promote(shadow.id, force=False, principal=_principal())
        outcome = await service.rollback()
        assert outcome.activated.id == current.id
        assert outcome.retired_id == shadow.id
        active = await service.active_generation()
        assert active is not None and active.id == current.id
    finally:
        await session.close()


async def test_shadow_evaluate_allows_coexisting_active_generations() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.gateway.application.services.shadow_evaluation_service import ShadowEvaluationService

    active_id, shadow_id = uuid4(), uuid4()
    session = AsyncMock()

    async def _scalar(statement, *args, **kwargs):
        rendered = str(statement)
        if "embedding_generations" in rendered:
            return active_id
        return 1.0

    session.scalar.side_effect = _scalar
    session.scalars.return_value = [active_id]

    async def single_generation_search(principal, spaces, query, generation_id, limit, minimum_score):
        return [SimpleNamespace(embedding_generation_id=generation_id, canonical_id=uuid4())]

    repository = SimpleNamespace(lexical_search=single_generation_search)
    service = ShadowEvaluationService(session, repository_factory=lambda: repository)
    report = await service.evaluate(_principal(), shadow_id)
    assert report.shadow_generation_id == shadow_id
    assert report.current_generation_id == active_id


async def test_shadow_evaluate_asserts_single_generation_per_result_page() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from src.gateway.application.services.shadow_evaluation_service import ShadowEvaluationService

    active_id, shadow_id = uuid4(), uuid4()
    session = AsyncMock()

    async def _scalar(statement, *args, **kwargs):
        rendered = str(statement)
        if "embedding_generations" in rendered:
            return active_id
        return 1.0

    session.scalar.side_effect = _scalar
    session.scalars.return_value = [active_id]

    async def mixed_search(principal, spaces, query, generation_id, limit, minimum_score):
        return [
            SimpleNamespace(embedding_generation_id=generation_id, canonical_id=uuid4()),
            SimpleNamespace(embedding_generation_id=uuid4(), canonical_id=uuid4()),
        ]

    repository = SimpleNamespace(lexical_search=mixed_search)
    service = ShadowEvaluationService(session, repository_factory=lambda: repository)
    with pytest.raises(ResourceConflictException) as excinfo:
        await service.evaluate(_principal(), shadow_id)
    assert "generation_ids" in excinfo.value.details


async def test_shadow_evaluate_recall_matching_uses_expected_item_ids() -> None:
    import src.gateway.application.services.shadow_evaluation_service as shadow_module
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import NAMESPACE_URL, uuid5

    from src.gateway.application.services.shadow_evaluation_service import ShadowEvaluationService

    active_id, shadow_id = uuid4(), uuid4()
    session = AsyncMock()
    session.scalars.return_value = [active_id]

    async def _scalar(statement, *args, **kwargs):
        rendered = str(statement)
        if "embedding_generations" in rendered:
            return active_id
        return 1.0

    session.scalar.side_effect = _scalar
    evaluation_slice = load_shadow_eval_slice()
    first_case = evaluation_slice.recall_cases[0]
    expected = uuid5(
        NAMESPACE_URL,
        f"retrieval-quality-v2:{first_case['space_index']}:{first_case['slot']}:item",
    )
    seen: list[UUID] = []

    async def recording_search(principal, spaces, query, generation_id, limit, minimum_score):
        seen.append(generation_id)
        if query == first_case["query"] and generation_id == shadow_id:
            return [SimpleNamespace(embedding_generation_id=shadow_id, canonical_id=expected)]
        if "ZQLX-nebula" in query or "QRXZ-krypton" in query or "WQVM-photon" in query:
            return []
        return [SimpleNamespace(embedding_generation_id=generation_id, canonical_id=uuid4())]

    repository = SimpleNamespace(lexical_search=recording_search)
    service = ShadowEvaluationService(session, repository_factory=lambda: repository)
    report = await service.evaluate(_principal(), shadow_id)
    assert shadow_module._expected_canonical_id(first_case) == expected
    assert seen and set(seen) == {active_id, shadow_id}
    assert report.shadow.missed and first_case["id"] not in report.shadow.missed
    assert report.current.missed and first_case["id"] in report.current.missed
    assert report.current_generation_id == active_id
    assert report.shadow_generation_id == shadow_id


async def test_lifecycle_status_payload_lists_shadow_generation(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        await _seed_generations(session)
        payload = await service.status_payload()
        assert payload["purpose"] == "retrieval"
        statuses = {generation["status"] for generation in payload["generations"]}
        assert {"active", "building"} <= statuses
    finally:
        await session.close()


async def test_pending_reembed_still_blocks_unforced_promote(factory) -> None:
    session, service = await _lifecycle(factory)
    try:
        _, shadow = await _seed_generations(session)
        session.add(
            MigrationBackfillRunModel(
                name="embedding_reembed:knowledge_revisions",
                phase="snapshot",
                rows_migrated=0,
            )
        )
        await session.flush()
        evaluator = _StubEvaluator(report=_report(shadow.id, allowed=True))
        service = EmbeddingGenerationLifecycleService(session, shadow_evaluator=evaluator)
        with pytest.raises(ResourceConflictException) as excinfo:
            await service.promote(shadow.id, force=False, principal=_principal())
        assert "pending_targets" in excinfo.value.details
        assert evaluator.calls == []
    finally:
        await session.close()
