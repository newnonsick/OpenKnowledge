from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import event, select

from src.gateway.domain.entities import KnowledgeItem as DomainKnowledgeItem, KnowledgeRevision as DomainKnowledgeRevision
from src.gateway.domain.identity import Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.database import principal_session
from src.gateway.infrastructure.persistence.identity_models import MemberModel, SpaceMembershipModel
from src.gateway.infrastructure.persistence.ingestion_models import EmbeddingGenerationModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import EMBED_DIM, KnowledgeItem, KnowledgeRevision, Workspace
from src.gateway.infrastructure.persistence.knowledge_repository import KnowledgeRepository
from src.gateway.infrastructure.persistence.principal_context import bind_principal, reset_principal
from src.gateway.infrastructure.persistence.retrieval_unit_repository import PostgresRetrievalUnitRepository
from tests.integration.postgres_test_database import isolated_postgres_database


async def test_multilingual_retrieval_is_authorized_bounded_and_vector_exactness_is_measurable() -> None:
    member_id = uuid4()
    other_member_id = uuid4()
    generation_id = uuid4()
    family_item_id = uuid4()
    family_revision_id = uuid4()
    mixed_item_id = uuid4()
    mixed_revision_id = uuid4()
    semantic_item_id = uuid4()
    semantic_revision_id = uuid4()
    hidden_item_id = uuid4()
    hidden_revision_id = uuid4()
    now = datetime.now(timezone.utc)
    query_vector = [1.0] + [0.0] * (EMBED_DIM - 1)

    async with isolated_postgres_database() as (engine, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(
                        id=member_id,
                        username="family-user",
                        username_normalized="family-user",
                        display_name="Family User",
                        status="active",
                        system_role="member",
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=other_member_id,
                        username="hidden-user",
                        username_normalized="hidden-user",
                        display_name="Hidden User",
                        status="active",
                        system_role="member",
                        force_password_change=False,
                    ),
                ]
            )
            session.add_all(
                [
                    Workspace(id="family", name="Family", created_by_member_id=member_id),
                    Workspace(id="hidden", name="Hidden", created_by_member_id=other_member_id),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    SpaceMembershipModel(space_id="family", member_id=member_id, role="owner"),
                    SpaceMembershipModel(space_id="hidden", member_id=other_member_id, role="owner"),
                    EmbeddingGenerationModel(
                        id=generation_id,
                        purpose="retrieval",
                        model_id="deterministic-test",
                        dimensions=EMBED_DIM,
                        status="active",
                        activated_at=now,
                    ),
                ]
            )
            for item_id, revision_id, space_id, title, content, embedding in (
                (family_item_id, family_revision_id, "family", "คู่มือครอบครัว", "ข้อมูลสุขภาพและตารางนัดหมายของครอบครัว", None),
                (mixed_item_id, mixed_revision_id, "family", "PostgreSQL ของครอบครัว", "PostgreSQL สำหรับครอบครัว รองรับ vector search", None),
                (semantic_item_id, semantic_revision_id, "family", "Semantic", "meaningful vector content", query_vector),
                (hidden_item_id, hidden_revision_id, "hidden", "Private English", "confidential orchard schedule", query_vector),
            ):
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
                session.add(item)
                await session.flush()
                revision = KnowledgeRevision(
                    id=revision_id,
                    item_id=item_id,
                    space_id=space_id,
                    version=1,
                    title=title,
                    content_hash=revision_id.hex.ljust(64, "0")[:64],
                    content=content,
                    tags=[],
                    embedding=embedding,
                    author="test",
                    author_member_id=member_id if space_id == "family" else other_member_id,
                )
                session.add(revision)
                await session.flush()
                item.current_revision_id = revision_id
                session.add(
                    RetrievalUnitModel(
                        space_id=space_id,
                        source_type="knowledge_revision",
                        knowledge_revision_id=revision_id,
                        embedding_generation_id=generation_id,
                        title=title,
                        content=content,
                        language="th" if space_id == "family" else "en",
                        source_metadata={"untrusted_item_id": str(uuid4())},
                        embedding=embedding,
                        active=True,
                    )
                )

        principal = Principal(
            subject_id=str(member_id),
            kind=PrincipalKind.API_KEY,
            system_role=SystemRole.MEMBER,
            scopes=frozenset({"knowledge:read", "knowledge:write"}),
        )
        repository = PostgresRetrievalUnitRepository(factory)
        retrieval_statements = 0

        def count_retrieval_statement(conn, cursor, statement, parameters, context, executemany):
            nonlocal retrieval_statements
            if "WITH query_value AS" in statement:
                retrieval_statements += 1

        event.listen(engine.sync_engine, "before_cursor_execute", count_retrieval_statement)
        try:
            thai = await repository.lexical_search(
                principal,
                ["family", "hidden"] + [f"space-{index}" for index in range(100)],
                "ครอบครัว",
                generation_id,
                20,
                0.01,
            )
            mixed = await repository.lexical_search(
                principal,
                ["family", "hidden"],
                "PostgreSQL ครอบครัว",
                generation_id,
                20,
                0.01,
            )
            hidden = await repository.lexical_search(
                principal,
                ["family", "hidden"],
                "confidential orchard",
                generation_id,
                20,
                0.01,
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count_retrieval_statement)

        approximate = await repository.vector_search(
            principal,
            ["family", "hidden"],
            query_vector,
            generation_id,
            20,
            0.9,
            False,
            100,
        )
        exact = await repository.vector_search(
            principal,
            ["family", "hidden"],
            query_vector,
            generation_id,
            20,
            0.9,
            True,
            100,
        )
        coverage = await repository.generation_coverage(
            principal,
            ["family", "hidden"],
            generation_id,
        )

        assert retrieval_statements == 3
        assert {item.canonical_id for item in thai} == {family_item_id, mixed_item_id}
        assert [item.canonical_id for item in mixed] == [mixed_item_id]
        assert hidden == []
        assert {item.canonical_id for item in approximate} == {semantic_item_id}
        assert {item.unit_id for item in approximate} == {item.unit_id for item in exact}
        assert approximate[0].citation_uri.startswith("aigw://spaces/family/knowledge/")
        assert coverage == 1.0 / 3.0

        projected_item_id = uuid4()
        first_projection_id = uuid4()
        second_projection_id = uuid4()
        knowledge_repository = KnowledgeRepository(factory)
        token = bind_principal(principal)
        try:
            await knowledge_repository.create_item(
                DomainKnowledgeItem(
                    id=projected_item_id,
                    workspace_id="family",
                    title="บันทึกใหม่",
                    content="เนื้อหารุ่นแรก",
                ),
                DomainKnowledgeRevision(
                    id=first_projection_id,
                    item_id=projected_item_id,
                    version=1,
                    title="บันทึกใหม่",
                    content="เนื้อหารุ่นแรก",
                    content_hash="a" * 64,
                    embedding=None,
                ),
            )
            await knowledge_repository.update_item_occ(
                projected_item_id,
                1,
                DomainKnowledgeRevision(
                    id=second_projection_id,
                    item_id=projected_item_id,
                    version=2,
                    title="บันทึกใหม่",
                    content="เนื้อหารุ่นสองค้นหาได้",
                    content_hash="b" * 64,
                    embedding=None,
                ),
                workspace_id="family",
            )
        finally:
            reset_principal(token)

        async with principal_session(factory, principal) as session:
            projections = list(
                await session.scalars(
                    select(RetrievalUnitModel)
                    .where(
                        RetrievalUnitModel.knowledge_revision_id.in_(
                            [first_projection_id, second_projection_id]
                        )
                    )
                    .order_by(RetrievalUnitModel.created_at)
                )
            )
        projected = await repository.lexical_search(
            principal,
            ["family"],
            "รุ่นสองค้นหาได้",
            generation_id,
            20,
            0.01,
        )

        assert len(projections) == 2
        assert projections[0].active is False
        assert projections[0].deactivated_at is not None
        assert projections[1].active is True
        assert projections[1].language == "th"
        assert {item.canonical_id for item in projected} == {projected_item_id}
