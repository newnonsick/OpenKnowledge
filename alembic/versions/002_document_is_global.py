"""Add is_global flag to document_files and document_chunks.

Revision ID: 002
Revises: 001
Create Date: 2026-08-16

The upload API already accepts ``is_global`` but the value was never
persisted, so documents flagged as globally shared were invisible to every
other workspace. This migration adds the flag to both tables and backfills
rows stored in the default (shared) workspace so existing behavior is
preserved.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'document_files',
        sa.Column('is_global', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )
    op.add_column(
        'document_chunks',
        sa.Column('is_global', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    )

    # Rows previously stored in the default workspace behaved as globally
    # shared in search; keep that behavior after the flag exists.
    op.execute("UPDATE document_files SET is_global = true WHERE workspace_id = 'global'")
    op.execute("UPDATE document_chunks SET is_global = true WHERE workspace_id = 'global'")


def downgrade() -> None:
    op.drop_column('document_chunks', 'is_global')
    op.drop_column('document_files', 'is_global')
