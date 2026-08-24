from src.gateway.infrastructure.persistence.models import Base


def test_high_volume_collections_have_keyset_pagination_indexes() -> None:
    expected = {
        "knowledge_items": {"ix_knowledge_items_space_active_page"},
        "documents": {"ix_documents_space_active_page"},
        "ingestion_jobs": {"ix_ingestion_jobs_space_state_page"},
        "personal_api_keys": {"ix_personal_api_keys_member_status_page"},
        "session_families": {"ix_session_families_member_page"},
        "audit_events": {"ix_audit_events_page"},
        "pending_ai_actions": {"ix_pending_ai_actions_actor_pending_page"},
    }

    for table_name, expected_names in expected.items():
        actual_names = {index.name for index in Base.metadata.tables[table_name].indexes}
        assert expected_names <= actual_names
