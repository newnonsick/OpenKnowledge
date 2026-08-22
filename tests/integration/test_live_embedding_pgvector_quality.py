from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from src.gateway.config import get_settings
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.adapters.http_embedding_client import HTTPEmbeddingClient
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from tests.integration.postgres_test_database import isolated_postgres_database


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval-evaluation-v1.json"


def _id(space_index: int, kind: str):
    return uuid5(NAMESPACE_URL, f"live-retrieval-quality-v1:{space_index}:{kind}")


@pytest.mark.live_provider
@pytest.mark.skipif(
    os.getenv("RUN_LIVE_PROVIDER_TESTS", "").lower() not in {"1", "true", "yes"},
    reason="Live provider quality tests require RUN_LIVE_PROVIDER_TESTS=true",
)
async def test_live_embedding_semantic_quality_through_authorized_pgvector_search() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    settings = get_settings()
    assert settings.embedding.dimension == EMBED_DIM
    cases = {int(case["space_index"]): case for case in fixture["cases"]}
    corpus = []
    for space_index in range(100):
        case = cases.get(space_index)
        if case:
            corpus.append(case["content"])
        elif space_index == 50:
            corpus.append("Disabled member account authorization exception for household API access")
        else:
            corpus.append(
                f"Neutral household archive record {space_index:03d} about furniture inventory"
            )
    client = HTTPEmbeddingClient()
    corpus_vectors = await client.embed_texts(corpus)
    query_vectors = await client.embed_texts(
        [case["semantic_query"] for case in fixture["cases"]]
    )
    await HTTPEmbeddingClient.close_shared_client()

    member_id = uuid4()
    hidden_member_id = uuid4()
    generation_id = uuid4()
    expected_ids = {
        case["id"]: _id(int(case["space_index"]), "item")
        for case in fixture["cases"]
    }
    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(
                        id=member_id,
                        username="live-quality-member",
                        username_normalized="live-quality-member",
                        display_name="Live Quality Member",
                        status="active",
                        system_role="member",
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=hidden_member_id,
                        username="live-quality-hidden",
                        username_normalized="live-quality-hidden",
                        display_name="Live Quality Hidden",
                        status="active",
                        system_role="member",
                        force_password_change=False,
                    ),
                ]
            )
            session.add_all(
                [
                    Workspace(
                        id=f"live-quality-{space_index:03d}",
                        name=f"Live Quality {space_index:03d}",
                        created_by_member_id=member_id if space_index < 50 else hidden_member_id,
                    )
                    for space_index in range(100)
                ]
            )
            await session.flush()
            session.add_all(
                [
                    SpaceMembershipModel(
                        space_id=f"live-quality-{space_index:03d}",
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
                    model_id=settings.embedding.model_id,
                    dimensions=EMBED_DIM,
                    status="active",
                    activated_at=datetime.now(timezone.utc),
                )
            )
            items = []
            revisions = []
            units = []
            for space_index, (content, embedding) in enumerate(zip(corpus, corpus_vectors)):
                space_id = f"live-quality-{space_index:03d}"
                item_id = _id(space_index, "item")
                revision_id = _id(space_index, "revision")
                case = cases.get(space_index)
                title = case["title"] if case else f"Neutral archive {space_index:03d}"
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
                        embedding=embedding,
                        author="live-quality-gate",
                        author_member_id=member_id if space_index < 50 else hidden_member_id,
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
            for space_index, item in enumerate(items):
                item.current_revision_id = _id(space_index, "revision")
            session.add_all(units)

        principal = Principal(
            subject_id=str(member_id),
            kind=PrincipalKind.API_KEY,
            system_role=SystemRole.MEMBER,
            scopes=frozenset({"knowledge:read"}),
        )
        repository = PostgresRetrievalUnitRepository(factory)
        requested_spaces = [f"live-quality-{space_index:03d}" for space_index in range(100)]
        semantic_matches = 0
        ann_matches = 0
        ann_overlap = []
        slice_results = {}
        for case, vector in zip(fixture["cases"], query_vectors):
            exact = await repository.vector_search(
                principal,
                requested_spaces,
                vector,
                generation_id,
                5,
                0.0,
                True,
                200,
            )
            approximate = await repository.vector_search(
                principal,
                requested_spaces,
                vector,
                generation_id,
                5,
                0.0,
                False,
                200,
            )
            expected = expected_ids[case["id"]]
            exact_ids = {hit.canonical_id for hit in exact}
            approximate_ids = {hit.canonical_id for hit in approximate}
            semantic_matches += int(expected in exact_ids)
            ann_matches += int(expected in approximate_ids)
            ann_overlap.append(
                len(exact_ids & approximate_ids) / max(len(exact_ids), 1)
            )
            slice_results[case["slice"]] = {
                "exact_target_found": expected in exact_ids,
                "ann_target_found": expected in approximate_ids,
                "ann_overlap": ann_overlap[-1],
            }
            assert all(hit.space_id < "live-quality-050" for hit in exact + approximate)

        case_count = len(fixture["cases"])
        assert semantic_matches / case_count >= fixture["minimum_semantic_recall"]
        assert ann_matches / case_count >= fixture["minimum_semantic_recall"]
        assert sum(ann_overlap) / case_count >= fixture["minimum_ann_recall"]
        evidence_path = os.getenv("LIVE_PROVIDER_EVIDENCE_FILE")
        if evidence_path:
            Path(evidence_path).write_text(
                json.dumps(
                    {
                        "fixture_version": fixture["version"],
                        "model_id": settings.embedding.model_id,
                        "semantic_recall": semantic_matches / case_count,
                        "ann_target_recall": ann_matches / case_count,
                        "ann_exact_overlap": sum(ann_overlap) / case_count,
                        "slices": slice_results,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
