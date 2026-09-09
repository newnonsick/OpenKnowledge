from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import random
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import event, text

from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from tests.integration.postgres_test_database import isolated_postgres_database


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval-evaluation-v1.json"


def _id(space_index: int, slot: int, kind: str):
    return uuid5(NAMESPACE_URL, f"retrieval-quality-v1:{space_index}:{slot}:{kind}")


def _query_vector() -> list[float]:
    rng = random.Random("retrieval-quality-query-v1")
    drawn = [rng.gauss(0.0, 1.0) for _ in range(EMBED_DIM)]
    norm = math.sqrt(sum(value * value for value in drawn))
    return [value / norm for value in drawn]


def _noise_direction() -> list[float]:
    query = _query_vector()
    rng = random.Random("retrieval-quality-noise-v1")
    drawn = [rng.gauss(0.0, 1.0) for _ in range(EMBED_DIM)]
    dot = sum(a * b for a, b in zip(drawn, query))
    orthogonal = [a - dot * b for a, b in zip(drawn, query)]
    norm = math.sqrt(sum(value * value for value in orthogonal))
    return [value / norm for value in orthogonal]


_NOISE_DIRECTION: list[float] | None = None


def _distractor_vector(space_index: int, slot: int) -> list[float]:
    query = _query_vector()
    rng = random.Random(f"retrieval-quality-distractor-v1:{space_index}:{slot}")
    drawn = [rng.gauss(0.0, 1.0) for _ in range(EMBED_DIM)]
    combined = [0.1 * a + b for a, b in zip(query, drawn)]
    norm = math.sqrt(sum(value * value for value in combined))
    return [value / norm for value in combined]


def _relevant_vector(space_index: int) -> list[float]:
    global _NOISE_DIRECTION
    if _NOISE_DIRECTION is None:
        _NOISE_DIRECTION = _noise_direction()
    query = _query_vector()
    magnitude = 0.001 if space_index < 20 else 0.02
    combined = [a + magnitude * b for a, b in zip(query, _NOISE_DIRECTION)]
    norm = math.sqrt(sum(value * value for value in combined))
    return [value / norm for value in combined]


def _plan_uses_index(value, index_name: str) -> bool:
    if isinstance(value, dict):
        if value.get("Index Name") == index_name:
            return True
        return any(_plan_uses_index(child, index_name) for child in value.values())
    if isinstance(value, list):
        return any(_plan_uses_index(child, index_name) for child in value)
    return False


async def test_versioned_multilingual_quality_and_filtered_ann_recall_across_100_spaces() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["version"] == 1
    cases = {
        (int(case["space_index"]), int(case["slot"])): case
        for case in fixture["cases"]
    }
    member_id = uuid4()
    hidden_member_id = uuid4()
    generation_id = uuid4()
    now = datetime.now(timezone.utc)
    expected_vector_ids = set()

    async with isolated_postgres_database() as (engine, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(
                        id=member_id,
                        username="quality-member",
                        username_normalized="quality-member",
                        display_name="Quality Member",
                        status="active",
                        system_role="member",
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=hidden_member_id,
                        username="quality-hidden",
                        username_normalized="quality-hidden",
                        display_name="Quality Hidden",
                        status="active",
                        system_role="member",
                        force_password_change=False,
                    ),
                ]
            )
            session.add_all(
                [
                    Workspace(
                        id=f"quality-{space_index:03d}",
                        name=f"Quality {space_index:03d}",
                        created_by_member_id=member_id if space_index < 50 else hidden_member_id,
                    )
                    for space_index in range(100)
                ]
            )
            await session.flush()
            session.add_all(
                [
                    SpaceMembershipModel(
                        space_id=f"quality-{space_index:03d}",
                        member_id=member_id if space_index < 50 else hidden_member_id,
                        role="owner",
                    )
                    for space_index in range(100)
                ]
            )
            session.add(
                EmbeddingGenerationModel(
                    id=generation_id,
                    purpose="retrieval",
                    model_id="quality-gate-deterministic",
                    dimensions=EMBED_DIM,
                    status="active",
                    activated_at=now,
                )
            )
            items = []
            item_revisions = []
            revisions = []
            units = []
            for space_index in range(100):
                space_id = f"quality-{space_index:03d}"
                author_id = member_id if space_index < 50 else hidden_member_id
                for slot in range(50):
                    item_id = _id(space_index, slot, "item")
                    revision_id = _id(space_index, slot, "revision")
                    case = cases.get((space_index, slot))
                    title = case["title"] if case else f"Neutral record {space_index}-{slot}"
                    content = case["content"] if case else f"neutral corpus token q{space_index:03d}s{slot:02d}"
                    if space_index == 50 and slot == 0:
                        content = "HIDDEN-ORCHID-XYZZY-5000"
                    embedding = (
                        _relevant_vector(space_index)
                        if slot == 1
                        else _distractor_vector(space_index, slot)
                    )
                    if space_index < 20 and slot == 1:
                        expected_vector_ids.add(item_id)
                    item = KnowledgeItem(
                        id=item_id,
                        workspace_id=space_id,
                        title=title,
                        content=content,
                        current_revision_id=None,
                        tags=[],
                        is_global=False,
                        is_deleted=False,
                    )
                    items.append(item)
                    item_revisions.append((item, revision_id))
                    revisions.append(
                        KnowledgeRevision(
                            id=revision_id,
                            item_id=item_id,
                            space_id=space_id,
                            version=1,
                            title=title,
                            content_hash=revision_id.hex.ljust(64, "0")[:64],
                            content=content,
                            tags=[],
                            embedding=None,
                            author="quality-gate",
                            author_member_id=author_id,
                        )
                    )
                    units.append(
                        RetrievalUnitModel(
                            space_id=space_id,
                            source_type="knowledge_revision",
                            knowledge_revision_id=revision_id,
                            embedding_generation_id=generation_id,
                            title=title,
                            content=content,
                            language=case["slice"] if case else "neutral",
                            embedding=embedding,
                            active=True,
                        )
                    )
            session.add_all(items)
            await session.flush()
            session.add_all(revisions)
            await session.flush()
            for item, revision_id in item_revisions:
                item.current_revision_id = revision_id
            session.add_all(units)

        async with factory.begin() as session:
            await session.execute(text("ANALYZE retrieval_units"))

        principal = Principal(
            subject_id=str(member_id),
            kind=PrincipalKind.API_KEY,
            system_role=SystemRole.MEMBER,
            scopes=frozenset({"knowledge:read"}),
        )
        repository = PostgresRetrievalUnitRepository(factory)
        requested_spaces = [f"quality-{space_index:03d}" for space_index in range(100)]
        retrieval_statements = 0

        def count_retrieval_statement(conn, cursor, statement, parameters, context, executemany):
            nonlocal retrieval_statements
            if "WITH query_value AS" in statement or "WITH nearest AS" in statement:
                retrieval_statements += 1

        event.listen(engine.sync_engine, "before_cursor_execute", count_retrieval_statement)
        try:
            lexical_hits = {}
            for case in fixture["cases"]:
                hits = await repository.lexical_search(
                    principal,
                    requested_spaces,
                    case["query"],
                    generation_id,
                    10,
                    0.01,
                )
                lexical_hits[case["id"]] = {
                    hit.canonical_id for hit in hits
                }
            hidden = await repository.lexical_search(
                principal,
                requested_spaces,
                "HIDDEN-ORCHID-XYZZY-5000",
                generation_id,
                10,
                0.01,
            )
            approximate = await repository.vector_search(
                principal,
                requested_spaces,
                _query_vector(),
                generation_id,
                20,
                0.99,
                False,
                1000,
            )
            exact = await repository.vector_search(
                principal,
                requested_spaces,
                _query_vector(),
                generation_id,
                20,
                0.99,
                True,
                1000,
            )
            vector_plan = await repository.vector_search_plan(
                principal,
                requested_spaces,
                _query_vector(),
                generation_id,
                20,
                0.99,
                1000,
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count_retrieval_statement)

        lexical_matches = 0
        for case in fixture["cases"]:
            expected = _id(int(case["space_index"]), int(case["slot"]), "item")
            lexical_matches += int(expected in lexical_hits[case["id"]])
        lexical_recall = lexical_matches / len(fixture["cases"])
        approximate_ids = {hit.canonical_id for hit in approximate}
        exact_ids = {hit.canonical_id for hit in exact}
        ann_recall = len(approximate_ids & exact_ids) / max(len(exact_ids), 1)

        assert lexical_recall >= fixture["minimum_lexical_recall"]
        assert hidden == []
        assert exact_ids == expected_vector_ids
        assert ann_recall >= fixture["minimum_ann_recall"]
        assert _plan_uses_index(vector_plan, "ix_retrieval_units_embedding")
        assert all(hit.space_id < "quality-050" for hit in approximate + exact)
        assert retrieval_statements == len(fixture["cases"]) + 4
