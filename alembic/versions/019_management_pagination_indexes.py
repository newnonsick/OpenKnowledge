from typing import Sequence, Union

from alembic import op


revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_knowledge_items_space_active_page",
        "knowledge_items",
        ["workspace_id", "is_deleted", "id"],
    )
    op.create_index(
        "ix_documents_space_active_page",
        "documents",
        ["space_id", "archived_at", "created_at", "id"],
    )
    op.create_index(
        "ix_ingestion_jobs_space_state_page",
        "ingestion_jobs",
        ["space_id", "state", "created_at", "id"],
    )
    op.create_index(
        "ix_personal_api_keys_member_status_page",
        "personal_api_keys",
        ["member_id", "status", "created_at", "id"],
    )
    op.create_index(
        "ix_session_families_member_page",
        "session_families",
        ["member_id", "created_at", "id"],
    )
    op.create_index("ix_audit_events_page", "audit_events", ["occurred_at", "id"])
    op.create_index(
        "ix_pending_ai_actions_actor_pending_page",
        "pending_ai_actions",
        ["actor_member_id", "state", "created_at", "id"],
    )
    op.create_index(
        "ix_audit_events_filter_page",
        "audit_events",
        ["action", "outcome", "resource_type", "occurred_at", "id"],
    )
    op.create_index(
        "ix_knowledge_items_tags_gin",
        "knowledge_items",
        ["tags"],
        postgresql_using="gin",
    )
    for name, table, column in (
        ("ix_workspaces_name_trgm", "workspaces", "name"),
        ("ix_members_username_trgm", "members", "username"),
        ("ix_members_display_name_trgm", "members", "display_name"),
        ("ix_knowledge_items_title_trgm", "knowledge_items", "title"),
        ("ix_knowledge_items_content_trgm", "knowledge_items", "content"),
        ("ix_documents_display_name_trgm", "documents", "display_name"),
        ("ix_document_revisions_filename_trgm", "document_revisions", "original_filename"),
        ("ix_personal_api_keys_name_trgm", "personal_api_keys", "name"),
        ("ix_audit_events_request_id_trgm", "audit_events", "request_id"),
    ):
        op.create_index(
            name,
            table,
            [column],
            postgresql_using="gin",
            postgresql_ops={column: "gin_trgm_ops"},
        )


def downgrade() -> None:
    for name, table in (
        ("ix_audit_events_request_id_trgm", "audit_events"),
        ("ix_personal_api_keys_name_trgm", "personal_api_keys"),
        ("ix_document_revisions_filename_trgm", "document_revisions"),
        ("ix_documents_display_name_trgm", "documents"),
        ("ix_knowledge_items_content_trgm", "knowledge_items"),
        ("ix_knowledge_items_title_trgm", "knowledge_items"),
        ("ix_members_display_name_trgm", "members"),
        ("ix_members_username_trgm", "members"),
        ("ix_workspaces_name_trgm", "workspaces"),
        ("ix_knowledge_items_tags_gin", "knowledge_items"),
        ("ix_audit_events_filter_page", "audit_events"),
        ("ix_pending_ai_actions_actor_pending_page", "pending_ai_actions"),
        ("ix_audit_events_page", "audit_events"),
        ("ix_session_families_member_page", "session_families"),
        ("ix_personal_api_keys_member_status_page", "personal_api_keys"),
        ("ix_ingestion_jobs_space_state_page", "ingestion_jobs"),
        ("ix_documents_space_active_page", "documents"),
        ("ix_knowledge_items_space_active_page", "knowledge_items"),
    ):
        op.drop_index(name, table_name=table)
