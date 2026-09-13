from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "023"
down_revision: Union[str, None] = "022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "pending_ai_actions",
        sa.Column("proposed_by_credential_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_pending_ai_actions_proposed_credential",
        "pending_ai_actions",
        ["proposed_by_credential_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_ai_actions_proposed_credential", table_name="pending_ai_actions")
    op.drop_column("pending_ai_actions", "proposed_by_credential_id")
