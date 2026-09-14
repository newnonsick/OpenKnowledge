from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import Double, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.gateway.domain.exceptions import AuthorizationException, ResourceConflictException
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.persistence.ingestion_models import (
    EmbeddingGenerationModel,
    RetrievalUnitModel,
)


SHADOW_EVAL_MAX_REGRESSION = 0.0
SHADOW_EVAL_MIN_NO_ANSWER_PRECISION = 1.0
SHADOW_EVAL_MIN_COVERAGE = 1.0

_EVAL_FIXTURE_PATH = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "retrieval-evaluation-v2.json"
_NON_RECALL_SLICES = ("thai_distractor", "code_distractor", "acl")


class _LexicalSearch(Protocol):
    async def __call__(
        self,
        principal: Principal,
        space_ids: list[str],
        query: str,
        generation_id: UUID,
        limit: int,
        minimum_score: float,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ShadowEvalThresholds:
    max_recall_regression: float = SHADOW_EVAL_MAX_REGRESSION
    min_no_answer_precision: float = SHADOW_EVAL_MIN_NO_ANSWER_PRECISION
    min_coverage: float = SHADOW_EVAL_MIN_COVERAGE


@dataclass(frozen=True, slots=True)
class ShadowEvalSlice:
    recall_cases: tuple[dict, ...]
    no_answer_cases: tuple[dict, ...]
    spaces: tuple[str, ...]
    minimum_recall: float


@dataclass(frozen=True, slots=True)
class ShadowEvalScores:
    lexical_recall: float
    recalled: int
    total: int
    missed: tuple[str, ...]
    no_answer_precision: float
    no_answer_quiet: int
    no_answer_total: int
    coverage: float


@dataclass(frozen=True, slots=True)
class ShadowEvalComparison:
    current: ShadowEvalScores
    shadow: ShadowEvalScores
    recall_regression: float
    allowed: bool
    block_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ShadowEvalReport:
    current_generation_id: UUID
    shadow_generation_id: UUID
    current: ShadowEvalScores
    shadow: ShadowEvalScores
    recall_regression: float
    allowed: bool
    block_reasons: tuple[str, ...]


def load_shadow_eval_slice() -> ShadowEvalSlice:
    fixture = json.loads(_EVAL_FIXTURE_PATH.read_text(encoding="utf-8"))
    cases = fixture["cases"]
    recall_cases = tuple(
        case
        for case in cases
        if not case.get("expect_empty") and case["slice"] not in _NON_RECALL_SLICES
    )
    no_answer_cases = tuple(case for case in cases if case.get("expect_empty"))
    spaces = tuple(f"slice-{index}" for index in sorted({case["space_index"] for case in cases}))
    return ShadowEvalSlice(
        recall_cases=recall_cases,
        no_answer_cases=no_answer_cases,
        spaces=spaces,
        minimum_recall=float(fixture["minimum_lexical_recall"]),
    )


def compare_shadow_scores(
    current: ShadowEvalScores,
    shadow: ShadowEvalScores,
    *,
    thresholds: ShadowEvalThresholds | None = None,
) -> ShadowEvalComparison:
    resolved = thresholds or ShadowEvalThresholds()
    regression = current.lexical_recall - shadow.lexical_recall
    reasons: list[str] = []
    if shadow.coverage < resolved.min_coverage:
        reasons.append("coverage")
    if regression > resolved.max_recall_regression:
        reasons.append("recall_regression")
    if shadow.no_answer_precision < resolved.min_no_answer_precision:
        reasons.append("no_answer_precision")
    return ShadowEvalComparison(
        current=current,
        shadow=shadow,
        recall_regression=regression,
        allowed=not reasons,
        block_reasons=tuple(reasons),
    )


def assert_single_generation(generation_ids: list[UUID | None]) -> UUID | None:
    distinct = {generation_id for generation_id in generation_ids if generation_id is not None}
    if len(distinct) > 1:
        raise ResourceConflictException(
            "Retrieval results mix multiple embedding generations.",
            details={"generation_ids": sorted(str(generation_id) for generation_id in distinct)},
        )
    return next(iter(distinct), None)


class ShadowEvaluationService:
    def __init__(
        self,
        session: AsyncSession,
        repository_factory=None,
    ) -> None:
        self._session = session
        self._repository_factory = repository_factory

    async def evaluate(
        self,
        principal: Principal,
        shadow_generation_id: UUID,
        *,
        thresholds: ShadowEvalThresholds | None = None,
    ) -> ShadowEvalReport:
        from src.gateway.infrastructure.persistence.retrieval_unit_repository import (
            PostgresRetrievalUnitRepository,
        )

        resolved = thresholds or ShadowEvalThresholds()
        active_id = await self._session.scalar(
            select(EmbeddingGenerationModel.id).where(
                EmbeddingGenerationModel.purpose == "retrieval",
                EmbeddingGenerationModel.status == "active",
            )
        )
        if active_id is None:
            raise AuthorizationException()
        if active_id == shadow_generation_id:
            raise ResourceConflictException(
                "Shadow evaluation requires a non-active generation.",
                details={"generation_id": str(shadow_generation_id)},
            )
        repository = (
            self._repository_factory()
            if self._repository_factory is not None
            else PostgresRetrievalUnitRepository()
        )
        lexical_search: _LexicalSearch | None = getattr(repository, "lexical_search", None)
        if lexical_search is None:
            raise AuthorizationException()
        evaluation_slice = load_shadow_eval_slice()
        current = await self._score_generation(
            lexical_search, principal, evaluation_slice, active_id
        )
        shadow = await self._score_generation(
            lexical_search, principal, evaluation_slice, shadow_generation_id
        )
        comparison = compare_shadow_scores(current, shadow, thresholds=resolved)
        return ShadowEvalReport(
            current_generation_id=active_id,
            shadow_generation_id=shadow_generation_id,
            current=current,
            shadow=shadow,
            recall_regression=comparison.recall_regression,
            allowed=comparison.allowed,
            block_reasons=comparison.block_reasons,
        )

    async def _score_generation(
        self,
        lexical_search: _LexicalSearch,
        principal: Principal,
        evaluation_slice: ShadowEvalSlice,
        generation_id: UUID,
    ) -> ShadowEvalScores:
        recalled = 0
        missed: list[str] = []
        for case in evaluation_slice.recall_cases:
            hits = await lexical_search(
                principal,
                list(evaluation_slice.spaces),
                case["query"],
                generation_id,
                10,
                0.01,
            )
            assert_single_generation([hit.embedding_generation_id for hit in hits])
            expected = _expected_canonical_id(case)
            if any(hit.canonical_id == expected for hit in hits):
                recalled += 1
            else:
                missed.append(case["id"])
        total = len(evaluation_slice.recall_cases)
        quiet = 0
        for case in evaluation_slice.no_answer_cases:
            hits = await lexical_search(
                principal,
                list(evaluation_slice.spaces),
                case["query"],
                generation_id,
                10,
                0.9,
            )
            assert_single_generation([hit.embedding_generation_id for hit in hits])
            if not hits:
                quiet += 1
        no_answer_total = len(evaluation_slice.no_answer_cases)
        coverage = await self._generation_coverage(generation_id, evaluation_slice.spaces)
        return ShadowEvalScores(
            lexical_recall=(recalled / total) if total else 1.0,
            recalled=recalled,
            total=total,
            missed=tuple(missed),
            no_answer_precision=(quiet / no_answer_total) if no_answer_total else 1.0,
            no_answer_quiet=quiet,
            no_answer_total=no_answer_total,
            coverage=coverage,
        )

    async def _generation_coverage(self, generation_id: UUID, spaces: tuple[str, ...]) -> float:
        if not spaces:
            return 0.0
        active_id = await self._session.scalar(
            select(EmbeddingGenerationModel.id).where(
                EmbeddingGenerationModel.purpose == "retrieval",
                EmbeddingGenerationModel.status == "active",
            )
        )
        shadow_units = await self._session.scalar(
            select(func.count())
            .select_from(RetrievalUnitModel)
            .where(
                RetrievalUnitModel.active.is_(True),
                RetrievalUnitModel.embedding_generation_id == generation_id,
                RetrievalUnitModel.space_id.in_(spaces),
            )
        )
        current_units = await self._session.scalar(
            select(func.count())
            .select_from(RetrievalUnitModel)
            .where(
                RetrievalUnitModel.active.is_(True),
                RetrievalUnitModel.embedding_generation_id == (active_id or generation_id),
                RetrievalUnitModel.space_id.in_(spaces),
            )
        )
        if not current_units:
            return 1.0
        return float(shadow_units or 0) / float(current_units)


def _expected_canonical_id(case: dict) -> UUID:
    return uuid5(NAMESPACE_URL, f"retrieval-quality-v2:{case['space_index']}:{case['slot']}:item")
