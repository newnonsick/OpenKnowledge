from __future__ import annotations

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import text

from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from tests.integration.postgres_test_database import isolated_postgres_database


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval-evaluation-v2.json"


def _id(space_index: int, slot: int, kind: str):
    return uuid5(NAMESPACE_URL, f"retrieval-quality-v2:{space_index}:{slot}:{kind}")


async def test_held_out_slices_acl_noanswer_stale_conflict_and_distractors() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["version"] == 2
    minimum_recall = float(fixture["minimum_lexical_recall"])
    cases = {case["id"]: case for case in fixture["cases"]}
    member_id = uuid4()
    hidden_member_id = uuid4()
    generation_id = uuid4()

    async with isolated_postgres_database() as (_, factory):
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
                    id=generation_id,
                    purpose="retrieval",
                    model_id="quality-gate-v2",
                    dimensions=EMBED_DIM,
                    status="active",
                )
            )
            items = []
            revisions = []
            units = []
            links = []
            for case in fixture["cases"]:
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
                        author="quality-gate-v2",
                        author_member_id=hidden_member_id if case["space_index"] == 4 else member_id,
                    )
                )
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

        principal = Principal(
            subject_id=str(member_id),
            kind=PrincipalKind.API_KEY,
            system_role=SystemRole.MEMBER,
            scopes=frozenset({"knowledge:read"}),
        )
        hidden_principal = Principal(
            subject_id=str(hidden_member_id),
            kind=PrincipalKind.API_KEY,
            system_role=SystemRole.MEMBER,
            scopes=frozenset({"knowledge:read"}),
        )
        repository = PostgresRetrievalUnitRepository(factory)
        spaces = [f"slice-{index}" for index in range(6)]

        baseline = fixture.get("baseline", {})
        baseline_recall = float(baseline.get("lexical_recall", minimum_recall))
        baseline_no_answer = float(baseline.get("no_answer_precision", 1.0))

        recall_cases = [
            case
            for case in fixture["cases"]
            if not case.get("expect_empty")
            and case["slice"] not in ("thai_distractor", "code_distractor", "acl")
        ]
        recalled = 0
        missed: list[str] = []
        for case in recall_cases:
            hits = await repository.lexical_search(principal, spaces, case["query"], generation_id, 10, 0.01)
            if _id(case["space_index"], case["slot"], "item") in {hit.canonical_id for hit in hits}:
                recalled += 1
            else:
                missed.append(case["id"])

        recall = recalled / len(recall_cases)
        assert recalled / len(recall_cases) >= minimum_recall
        assert recall >= baseline_recall, (
            f"lexical recall {recall:.3f} dropped below recorded baseline {baseline_recall:.3f}; missed={missed}"
        )

        acl_cases = [case for case in fixture["cases"] if case["slice"] == "acl"]
        for acl_case in acl_cases:
            if acl_case.get("visible_to") == "hidden_member":
                visible = await repository.lexical_search(
                    hidden_principal, spaces, acl_case["query"], generation_id, 10, 0.01
                )
                assert _id(acl_case["space_index"], acl_case["slot"], "item") in {
                    hit.canonical_id for hit in visible
                }, acl_case["id"]
                leaked = await repository.lexical_search(
                    principal, spaces, acl_case["query"], generation_id, 10, 0.01
                )
                assert _id(acl_case["space_index"], acl_case["slot"], "item") not in {
                    hit.canonical_id for hit in leaked
                }, acl_case["id"]
            else:
                own = await repository.lexical_search(
                    principal, spaces, acl_case["query"], generation_id, 10, 0.01
                )
                assert _id(acl_case["space_index"], acl_case["slot"], "item") in {
                    hit.canonical_id for hit in own
                }, acl_case["id"]

        revoked_cases = [case for case in fixture["cases"] if case.get("revoked") is True]
        assert revoked_cases, "fixture must cover acl-revocation"
        for revoked_case in revoked_cases:
            revoked_item = _id(revoked_case["space_index"], revoked_case["slot"], "item")
            async with factory.begin() as session:
                session.add(
                    SpaceMembershipModel(
                        space_id=f"slice-{revoked_case['space_index']}",
                        member_id=member_id,
                        role="reader",
                    )
                )
            granted = await repository.lexical_search(
                principal, spaces, revoked_case["query"], generation_id, 10, 0.01
            )
            assert revoked_item in {hit.canonical_id for hit in granted}, revoked_case["id"]
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "DELETE FROM space_memberships WHERE space_id = :space_id AND member_id = :member_id"
                    ),
                    {
                        "space_id": f"slice-{revoked_case['space_index']}",
                        "member_id": member_id,
                    },
                )
            after_revoke = await repository.lexical_search(
                principal, spaces, revoked_case["query"], generation_id, 10, 0.01
            )
            assert revoked_item not in {hit.canonical_id for hit in after_revoke}, revoked_case["id"]

        empty_cases = [case for case in fixture["cases"] if case.get("expect_empty")]
        assert empty_cases, "fixture must cover no-answer"
        quiet = 0
        for empty_case in empty_cases:
            no_answer = await repository.lexical_search(
                principal, spaces, empty_case["query"], generation_id, 10, 0.9
            )
            if not no_answer:
                quiet += 1
        no_answer_precision = quiet / len(empty_cases)
        assert no_answer_precision >= baseline_no_answer, (
            f"no-answer precision {no_answer_precision:.3f} dropped below recorded baseline "
            f"{baseline_no_answer:.3f}"
        )

        distractor_cases = [case for case in fixture["cases"] if case.get("distractor_for")]
        assert distractor_cases, "fixture must cover distractors"
        for distractor_case in distractor_cases:
            target = cases[distractor_case["distractor_for"]]
            distractor_hits = await repository.lexical_search(
                principal, spaces, target["query"], generation_id, 10, 0.0
            )
            found = {hit.canonical_id for hit in distractor_hits}
            assert _id(target["space_index"], target["slot"], "item") in found, distractor_case["id"]
            assert _id(distractor_case["space_index"], distractor_case["slot"], "item") in found, (
                distractor_case["id"]
            )

        stale_cases = [case for case in fixture["cases"] if case.get("superseded_by")]
        assert stale_cases, "fixture must cover stale/superseded"
        for stale_case in stale_cases:
            current = cases[stale_case["superseded_by"]]
            conflict_hits = await repository.lexical_search(
                principal, spaces, current["query"], generation_id, 10, 0.01
            )
            conflict_found = {hit.canonical_id for hit in conflict_hits}
            assert _id(current["space_index"], current["slot"], "item") in conflict_found, stale_case["id"]
            assert _id(stale_case["space_index"], stale_case["slot"], "item") in conflict_found, (
                stale_case["id"]
            )
