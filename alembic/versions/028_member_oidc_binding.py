from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "028"
down_revision: Union[str, None] = "027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("members", sa.Column("oidc_subject", sa.Text(), nullable=True))
    op.add_column("members", sa.Column("oidc_issuer", sa.String(length=500), nullable=True))
    op.create_unique_constraint(
        "uq_members_oidc_issuer_subject", "members", ["oidc_issuer", "oidc_subject"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_members_oidc_issuer_subject", "members", type_="unique")
    op.drop_column("members", "oidc_issuer")
    op.drop_column("members", "oidc_subject")
