from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_members_status", "members", type_="check")
    op.execute("UPDATE members SET status = 'pending' WHERE status = 'invited'")
    op.create_check_constraint(
        "ck_members_status",
        "members",
        "status IN ('pending','active','disabled')",
    )
    op.add_column(
        "mfa_factors",
        sa.Column("encryption_key_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("mfa_factors", "encryption_key_version")
    op.drop_constraint("ck_members_status", "members", type_="check")
    op.execute("UPDATE members SET status = 'invited' WHERE status = 'pending'")
    op.create_check_constraint(
        "ck_members_status",
        "members",
        "status IN ('invited','active','disabled')",
    )
