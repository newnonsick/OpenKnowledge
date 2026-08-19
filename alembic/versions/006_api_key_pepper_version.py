from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "personal_api_keys",
        sa.Column("pepper_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint(
        "ck_personal_api_keys_pepper_version",
        "personal_api_keys",
        "pepper_version > 0",
    )


def downgrade() -> None:
    connection = op.get_bind()
    incompatible = connection.execute(
        sa.text(
            "SELECT count(*) FROM personal_api_keys WHERE pepper_version <> 1"
        )
    ).scalar_one()
    if incompatible:
        raise RuntimeError(
            "Cannot downgrade while API keys use a non-v1 pepper; revoke or rotate them first"
        )
    op.drop_constraint(
        "ck_personal_api_keys_pepper_version",
        "personal_api_keys",
        type_="check",
    )
    op.drop_column("personal_api_keys", "pepper_version")
