from typing import Sequence, Union

from alembic import op


revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS knowledge_revisions_member_update ON knowledge_revisions"
    )
    op.execute(
        "DROP POLICY IF EXISTS knowledge_revisions_member_delete ON knowledge_revisions"
    )


def downgrade() -> None:
    revision_space = (
        "(SELECT item.workspace_id FROM knowledge_items item "
        "WHERE item.id = knowledge_revisions.item_id)"
    )
    op.execute(
        "CREATE POLICY knowledge_revisions_member_update ON knowledge_revisions "
        f"FOR UPDATE USING (gateway_has_space_role({revision_space}, ARRAY['owner','editor','reader'])) "
        f"WITH CHECK (gateway_has_space_role({revision_space}, ARRAY['owner','editor']))"
    )
    op.execute(
        "CREATE POLICY knowledge_revisions_member_delete ON knowledge_revisions "
        f"FOR DELETE USING (gateway_has_space_role({revision_space}, ARRAY['owner','editor']))"
    )
