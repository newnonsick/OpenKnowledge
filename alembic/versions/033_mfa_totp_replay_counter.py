from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "033"
down_revision: Union[str, None] = "032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mfa_factors",
        sa.Column("last_verified_counter", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mfa_factors", "last_verified_counter")
