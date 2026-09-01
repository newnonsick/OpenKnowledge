from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "021"
down_revision: Union[str, None] = "020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_mfa_factors_member_active",
        "mfa_factors",
        ["member_id"],
        postgresql_where=sa.text("confirmed_at IS NOT NULL AND retired_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_mfa_factors_member_active", table_name="mfa_factors")
