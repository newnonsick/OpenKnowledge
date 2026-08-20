from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_ingestion_jobs_request_fingerprint",
        "ingestion_jobs",
        "request_fingerprint IS NULL OR length(request_fingerprint) = 64",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_ingestion_jobs_request_fingerprint",
        "ingestion_jobs",
        type_="check",
    )
    op.drop_column("ingestion_jobs", "request_fingerprint")
