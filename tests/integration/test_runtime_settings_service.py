from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.gateway.application.services.runtime_settings_service import RuntimeSettingsService, RuntimeSettingsValues
from src.gateway.application.services.session_service import SessionService
from src.gateway.domain.exceptions import AuthorizationException, ConcurrencyConflictException
from src.gateway.domain.identity import MemberStatus, Principal, PrincipalKind, SystemRole
from src.gateway.infrastructure.persistence.identity_models import AuditEventModel, MemberModel
from tests.integration.postgres_test_database import isolated_postgres_database


def principal(member_id, role):
    return Principal(
        subject_id=str(member_id),
        kind=PrincipalKind.SESSION,
        system_role=role,
        scopes=frozenset({"*"}),
    )


async def test_safe_runtime_setting_draft_activation_and_optimistic_concurrency() -> None:
    now = datetime.now(timezone.utc)
    admin_id = uuid4()
    member_id = uuid4()

    async with isolated_postgres_database() as (_, factory):
        async with factory.begin() as session:
            session.add_all(
                [
                    MemberModel(
                        id=admin_id,
                        username="admin",
                        username_normalized="admin",
                        display_name="Admin",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.SUPER_ADMIN.value,
                        force_password_change=False,
                    ),
                    MemberModel(
                        id=member_id,
                        username="member",
                        username_normalized="member",
                        display_name="Member",
                        status=MemberStatus.ACTIVE.value,
                        system_role=SystemRole.MEMBER.value,
                        force_password_change=False,
                    ),
                ]
            )
            await session.flush()
            admin_session = await SessionService(session).issue(
                principal(admin_id, SystemRole.SUPER_ADMIN),
                now=now,
                step_up_at=now,
            )
            member_session = await SessionService(session).issue(
                principal(member_id, SystemRole.MEMBER),
                now=now,
                step_up_at=now,
            )

        async with factory.begin() as session:
            service = RuntimeSettingsService(session)
            active = await service.active()
            assert active.revision == 0
            assert active.values.retrieval.limit == 10
            with pytest.raises(AuthorizationException):
                await service.create_draft(
                    principal(member_id, SystemRole.MEMBER),
                    family_id=member_session.family_id,
                    base_revision=0,
                    values=RuntimeSettingsValues(),
                    reason="member denied",
                    request_id="settings-denied",
                    now=now,
                )

        values = RuntimeSettingsValues.model_validate(
            {
                "retrieval": {
                    "limit": 12,
                    "branch_limit": 60,
                    "lexical_weight": 1.2,
                    "vector_weight": 0.8,
                    "rrf_k": 50,
                    "minimum_lexical_score": 0.02,
                    "minimum_vector_similarity": 0.6,
                    "max_hits_per_source": 3,
                    "active_space_boost": 0.1,
                    "semantic_policy": "prefer",
                    "hnsw_ef_search": 120,
                }
            }
        )
        async with factory.begin() as session:
            service = RuntimeSettingsService(session)
            draft = await service.create_draft(
                principal(admin_id, SystemRole.SUPER_ADMIN),
                family_id=admin_session.family_id,
                base_revision=0,
                values=values,
                reason="Tune family retrieval",
                request_id="settings-draft",
                now=now,
            )
            activated = await service.activate(
                principal(admin_id, SystemRole.SUPER_ADMIN),
                family_id=admin_session.family_id,
                draft_id=draft.id,
                expected_active_revision=0,
                reason="Measured retrieval improvement",
                request_id="settings-activate",
                now=now,
            )
            assert activated.revision == 1
            assert activated.values.retrieval.limit == 12
            with pytest.raises(ConcurrencyConflictException):
                await service.activate(
                    principal(admin_id, SystemRole.SUPER_ADMIN),
                    family_id=admin_session.family_id,
                    draft_id=draft.id,
                    expected_active_revision=0,
                    reason="stale retry",
                    request_id="settings-stale",
                    now=now,
                )

        async with factory.begin() as session:
            active = await RuntimeSettingsService(session).active()
            audit = await session.scalar(
                select(AuditEventModel).where(AuditEventModel.request_id == "settings-activate")
            )
            assert active.revision == 1
            assert active.values.retrieval.vector_weight == 0.8
            assert audit is not None
            assert audit.details["old_revision"] == 0
            assert audit.details["new_revision"] == 1


def test_runtime_settings_reject_unknown_fields_and_unsafe_ranges() -> None:
    with pytest.raises(Exception):
        RuntimeSettingsValues.model_validate({"free_form_prompt": "do anything"})
    with pytest.raises(Exception):
        RuntimeSettingsValues.model_validate({"retrieval": {"active_space_boost": 2.0}})
    with pytest.raises(Exception):
        RuntimeSettingsValues.model_validate({"retrieval": {"lexical_weight": 0, "vector_weight": 0}})
