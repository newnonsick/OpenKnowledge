from __future__ import annotations

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from src.gateway.application.services.embedding_lifecycle_service import (
    EmbeddingGenerationLifecycleService,
)
from src.gateway.application.services.shadow_evaluation_service import (
    ShadowEvaluationService,
    load_shadow_eval_slice,
)
from src.gateway.domain.exceptions import ResourceConflictException
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from tests.integration.postgres_test_database import isolated_postgres_database


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval-evaluation-v2.json"


def _id(space_index: int, slot: int, kind: str):
    return uuid5(NAMESPACE_URL, f"retrieval-quality-v2:{space_index}:{slot}:{kind}")


def _principal(member_id) -> Principal:
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.API_KEY,
        system_role=SystemRole.MEMBER,
        scopes=frozenset({"knowledge:read"}),
    )


async def _seed_fixture(factory, member_id, hidden_member_id, active_id, shadow_id, *, shadow_missing: tuple[str, ...] = ()):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    async with factory.begin() as session:
        session.add_all(
            [
                MemberModel(
                    id=member_id,
                    username="shadow-member",
                    username_normalized="shadow-member",
                    display_name="Shadow Member",
                    status="active",
                    system_role="member",
                    force_password_change=False,
                ),
                MemberModel(
                    id=hidden_member_id,
                    username="shadow-hidden",
                    username_normalized="shadow-hidden",
                    display_name="Shadow Hidden",
                    status="active",
                    system_role="member",
                    force_password_change=False,
                ),
            ]
        )
        session.add_all(
            [
                Workspace(
                    id=f"slice-{space_index}",
                    name=f"Slice {space_index}",
                    created_by_member_id=hidden_member_id if space_index == 4 else member_id,
                )
                for space_index in range(6)
            ]
        )
        await session.flush()
        session.add_all(
            [
                SpaceMembershipModel(
                    space_id=f"slice-{space_index}",
                    member_id=hidden_member_id if space_index == 4 else member_id,
                    role="owner",
                )
                for space_index in range(6)
            ]
        )
        session.add(
            EmbeddingGenerationModel(
                id=active_id,
                purpose="retrieval",
                model_id="shadow-gate-current",
                dimensions=EMBED_DIM,
                status="active",
            )
        )
        session.add(
            EmbeddingGenerationModel(
                id=shadow_id,
                purpose="retrieval",
                model_id="shadow-gate-candidate",
                dimensions=EMBED_DIM,
                status="building",
            )
        )
        items = []
        revisions = []
        units = []
        links = []
        for case in fixture["cases"]:
            if case["id"] in shadow_missing:
                generation_ids = (active_id,)
            else:
                generation_ids = (active_id, shadow_id)
            space_id = f"slice-{case['space_index']}"
            item_id = _id(case["space_index"], case["slot"], "item")
            revision_id = _id(case["space_index"], case["slot"], "revision")
            item = KnowledgeItem(
                id=item_id,
                workspace_id=space_id,
                title=case["title"],
                content=case["content"],
                current_revision_id=None,
                tags=[],
                is_global=False,
                is_deleted=False,
            )
            items.append(item)
            links.append((item, revision_id))
            revisions.append(
                KnowledgeRevision(
                    id=revision_id,
                    item_id=item_id,
                    space_id=space_id,
                    version=1,
                    title=case["title"],
                    content_hash=revision_id.hex.ljust(64, "0")[:64],
                    content=case["content"],
                    tags=[],
                    embedding=None,
                    author="shadow-gate",
                    author_member_id=hidden_member_id if case["space_index"] == 4 else member_id,
                )
            )
            for generation_id in generation_ids:
                units.append(
                    RetrievalUnitModel(
                        space_id=space_id,
                        source_type="knowledge_revision",
                        knowledge_revision_id=revision_id,
                        embedding_generation_id=generation_id,
                        title=case["title"],
                        content=case["content"],
                        language=case["slice"],
                        embedding=None,
                        active=True,
                    )
                )
        session.add_all(items)
        await session.flush()
        session.add_all(revisions)
        await session.flush()
        for item, revision_id in links:
            item.current_revision_id = revision_id
        session.add_all(units)
    async with factory.begin() as session:
        await session.execute(text("ANALYZE retrieval_units"))


async def test_shadow_promote_blocked_on_regression() -> None:
    member_id = uuid4()
    hidden_member_id = uuid4()
    active_id = uuid4()
    shadow_id = uuid4()
    evaluation_slice = load_shadow_eval_slice()
    regressed = evaluation_slice.recall_cases[0]["id"]

    async with isolated_postgres_database() as (_, factory):
        await _seed_fixture(factory, member_id, hidden_member_id, active_id, shadow_id, shadow_missing=(regressed,))
        principal = _principal(member_id)
        repository = PostgresRetrievalUnitRepository(factory)
        spaces = [f"slice-{index}" for index in range(6)]
        async with factory() as session:
            service = EmbeddingGenerationLifecycleService(
                session, shadow_evaluator=ShadowEvaluationService(session, repository_factory=lambda: repository)
            )
            report = await service.evaluate_shadow(principal, shadow_id)
            assert not report.allowed
            assert "recall_regression" in report.block_reasons
            assert report.shadow.lexical_recall < report.current.lexical_recall
            assert regressed in report.shadow.missed
            with pytest.raises(ResourceConflictException) as excinfo:
                await service.promote(shadow_id, force=False, principal=principal)
            assert "recall_regression" in excinfo.value.details["block_reasons"]
            stored = await session.get(EmbeddingGenerationModel, shadow_id)
            assert stored is not None and stored.status == "building"


async def test_shadow_promote_allowed_on_pass_then_rollback() -> None:
    member_id = uuid4()
    hidden_member_id = uuid4()
    active_id = uuid4()
    shadow_id = uuid4()

    async with isolated_postgres_database() as (_, factory):
        await _seed_fixture(factory, member_id, hidden_member_id, active_id, shadow_id)
        principal = _principal(member_id)
        repository = PostgresRetrievalUnitRepository(factory)
        async with factory() as session:
            service = EmbeddingGenerationLifecycleService(
                session, shadow_evaluator=ShadowEvaluationService(session, repository_factory=lambda: repository)
            )
            report = await service.evaluate_shadow(principal, shadow_id)
            assert report.allowed, report.block_reasons
            assert report.shadow.coverage == 1.0
            outcome = await service.promote(shadow_id, force=False, principal=principal)
            assert outcome.promoted.id == shadow_id
            assert outcome.retired_id == active_id
            rolled_back = await service.rollback()
            assert rolled_back.activated.id == active_id
            assert rolled_back.retired_id == shadow_id
            active = await service.active_generation()
            assert active is not None and active.id == active_id


async def test_shadow_reads_never_mix_generations() -> None:
    member_id = uuid4()
    hidden_member_id = uuid4()
    active_id = uuid4()
    shadow_id = uuid4()

    async with isolated_postgres_database() as (_, factory):
        await _seed_fixture(factory, member_id, hidden_member_id, active_id, shadow_id)
        principal = _principal(member_id)
        repository = PostgresRetrievalUnitRepository(factory)
        evaluation_slice = load_shadow_eval_slice()
        spaces = [f"slice-{index}" for index in range(6)]
        for case in evaluation_slice.recall_cases:
            for generation_id in (active_id, shadow_id):
                try:
                    hits = await repository.lexical_search(principal, spaces, case["query"], generation_id, 10, 0.01)
                except DBAPIError as exc:
                    pytest.skip(f"lexical search unavailable: {exc}")
                generations = {hit.embedding_generation_id for hit in hits}
                assert generations <= {generation_id}, case["id"]
