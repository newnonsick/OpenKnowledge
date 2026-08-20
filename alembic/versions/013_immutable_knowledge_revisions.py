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
    op.execute(
        "CREATE POLICY knowledge_revisions_member_update ON knowledge_revisions "
        "FOR UPDATE USING (gateway_has_space_role(space_id, ARRAY['owner','editor','reader'])) "
        "WITH CHECK (gateway_has_space_role(space_id, ARRAY['owner','editor']))"
    )
    op.execute(
        "CREATE POLICY knowledge_revisions_member_delete ON knowledge_revisions "
        "FOR DELETE USING (gateway_has_space_role(space_id, ARRAY['owner','editor']))"
    )
