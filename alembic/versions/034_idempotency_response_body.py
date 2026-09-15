from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "034"
down_revision: Union[str, None] = "033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "idempotency_records",
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("idempotency_records", "response_body")
