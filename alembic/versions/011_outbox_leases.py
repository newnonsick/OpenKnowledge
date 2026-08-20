from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_job_outbox_state", "job_outbox", type_="check")
    op.add_column("job_outbox", sa.Column("lease_owner", sa.String(length=255), nullable=True))
    op.add_column("job_outbox", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_outbox", sa.Column("claim_token", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_check_constraint(
        "ck_job_outbox_state",
        "job_outbox",
        "state IN ('pending','publishing','published','failed')",
    )
    op.create_check_constraint(
        "ck_job_outbox_claim",
        "job_outbox",
        "(state = 'publishing' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND claim_token IS NOT NULL) OR "
        "(state <> 'publishing' AND lease_owner IS NULL AND lease_expires_at IS NULL AND claim_token IS NULL)",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE job_outbox SET state = 'pending', lease_owner = NULL, "
        "lease_expires_at = NULL, claim_token = NULL "
        "WHERE state = 'publishing'"
    )
    op.drop_constraint("ck_job_outbox_claim", "job_outbox", type_="check")
    op.drop_constraint("ck_job_outbox_state", "job_outbox", type_="check")
    op.create_check_constraint(
        "ck_job_outbox_state",
        "job_outbox",
        "state IN ('pending','published','failed')",
    )
    op.drop_column("job_outbox", "claim_token")
    op.drop_column("job_outbox", "lease_expires_at")
    op.drop_column("job_outbox", "lease_owner")
