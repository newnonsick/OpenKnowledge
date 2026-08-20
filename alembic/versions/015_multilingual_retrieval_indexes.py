from typing import Sequence, Union

from alembic import op


revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_retrieval_units_content_trgm ON retrieval_units "
        "USING gin (content gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_retrieval_units_title_trgm ON retrieval_units "
        "USING gin (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_retrieval_units_active_generation_space "
        "ON retrieval_units (embedding_generation_id, space_id) WHERE active"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_retrieval_units_active_generation_space",
        table_name="retrieval_units",
    )
    op.drop_index("ix_retrieval_units_title_trgm", table_name="retrieval_units")
    op.drop_index("ix_retrieval_units_content_trgm", table_name="retrieval_units")
