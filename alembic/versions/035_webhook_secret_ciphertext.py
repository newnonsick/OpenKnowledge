from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "webhook_subscriptions",
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "webhook_subscriptions",
        sa.Column("secret_key_version", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("webhook_subscriptions", "secret_key_version")
    op.drop_column("webhook_subscriptions", "secret_ciphertext")
