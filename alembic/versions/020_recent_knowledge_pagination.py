from typing import Sequence, Union

from alembic import op


revision: str = "020"
down_revision: Union[str, None] = "019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_knowledge_items_space_active_page", table_name="knowledge_items")
    op.create_index(
        "ix_knowledge_items_space_active_page",
        "knowledge_items",
        ["workspace_id", "is_deleted", "updated_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_items_space_active_page", table_name="knowledge_items")
    op.create_index(
        "ix_knowledge_items_space_active_page",
        "knowledge_items",
        ["workspace_id", "is_deleted", "id"],
    )
