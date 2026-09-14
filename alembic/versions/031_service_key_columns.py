from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "031"
down_revision: Union[str, None] = "030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "personal_api_keys",
        sa.Column("is_service", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "personal_api_keys",
        sa.Column("service_profile", sa.String(32), nullable=True),
    )
    op.create_check_constraint(
        "ck_personal_api_keys_service_profile",
        "personal_api_keys",
        "service_profile IS NULL OR service_profile IN ('reader','project_contributor','trusted_maintainer','import_worker')",
    )
    op.create_check_constraint(
        "ck_personal_api_keys_service_kind",
        "personal_api_keys",
        "(is_service = false AND service_profile IS NULL) OR (is_service = true)",
    )
    op.execute(
        "UPDATE personal_api_keys key SET is_service = true, service_profile = audit.details->>'permission_profile' "
        "FROM audit_events audit "
        "WHERE audit.resource_type = 'api_key' "
        "AND audit.resource_id = key.id::text "
        "AND audit.action = 'service_key.created' "
        "AND (audit.details->>'service_kind') = 'service'"
    )
    op.execute("REVOKE ALL ON personal_api_keys FROM PUBLIC")


def downgrade() -> None:
    op.drop_constraint("ck_personal_api_keys_service_kind", "personal_api_keys", type_="check")
    op.drop_constraint("ck_personal_api_keys_service_profile", "personal_api_keys", type_="check")
    op.drop_column("personal_api_keys", "service_profile")
    op.drop_column("personal_api_keys", "is_service")
