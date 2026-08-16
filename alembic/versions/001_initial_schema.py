"""Initial schema migration: pgvector extension, workspaces, knowledge, and document tables.

Revision ID: 001
Revises: 
Create Date: 2026-08-15
"""
from typing import Sequence, Union

from alembic import op
import pgvector.sqlalchemy
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from src.gateway.config import settings

revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

embed_dim = getattr(settings.embedding, "dimension", 768) if hasattr(settings, "embedding") else 768


def upgrade() -> None:
    # 1. Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    # 2. Create workspaces table
    op.create_table(
        'workspaces',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # 3. Create knowledge_items table (current_revision_id FK added subsequently)
    op.create_table(
        'knowledge_items',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('workspace_id', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=500), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('current_revision_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('is_global', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('is_deleted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_knowledge_items_workspace_id', 'knowledge_items', ['workspace_id'])
    op.create_index('ix_knowledge_items_workspace_deleted', 'knowledge_items', ['workspace_id', 'is_deleted'])
    op.create_index('ix_knowledge_items_global_deleted', 'knowledge_items', ['is_global', 'is_deleted'])

    # 4. Create knowledge_revisions table
    op.create_table(
        'knowledge_revisions',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('item_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('content_hash', sa.String(length=64), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', pgvector.sqlalchemy.Vector(embed_dim), nullable=True),
        sa.Column('author', sa.String(length=255), server_default=sa.text("'system'"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['knowledge_items.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_knowledge_revisions_item_id', 'knowledge_revisions', ['item_id'])
    op.create_index('ix_knowledge_revisions_item_version', 'knowledge_revisions', ['item_id', 'version'], unique=True)
    op.create_index(
        'ix_knowledge_revisions_embedding',
        'knowledge_revisions',
        ['embedding'],
        postgresql_using='hnsw',
        postgresql_with={'m': 16, 'ef_construction': 64},
        postgresql_ops={'embedding': 'vector_cosine_ops'},
    )

    # 5. Add circular FK constraint on knowledge_items.current_revision_id
    op.create_foreign_key(
        'fk_knowledge_items_current_revision_id',
        'knowledge_items',
        'knowledge_revisions',
        ['current_revision_id'],
        ['id'],
        ondelete='SET NULL',
        use_alter=True,
    )

    # 6. Create document_files table
    op.create_table(
        'document_files',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('workspace_id', sa.String(length=64), nullable=False),
        sa.Column('filename', sa.String(length=500), nullable=False),
        sa.Column('file_path', sa.String(length=1000), nullable=False),
        sa.Column('file_size', sa.BigInteger(), nullable=False),
        sa.Column('mime_type', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_document_files_workspace_id', 'document_files', ['workspace_id'])
    op.create_index('ix_document_files_workspace_created', 'document_files', ['workspace_id', 'created_at'])

    # 7. Create document_chunks table
    op.create_table(
        'document_chunks',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('document_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('workspace_id', sa.String(length=64), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', pgvector.sqlalchemy.Vector(embed_dim), nullable=True),
        sa.Column(
            'tsv',
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=True,
        ),
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['document_files.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_document_chunks_document_id', 'document_chunks', ['document_id'])
    op.create_index('ix_document_chunks_workspace_id', 'document_chunks', ['workspace_id'])
    op.create_index('ix_document_chunks_doc_chunk', 'document_chunks', ['document_id', 'chunk_index'])
    op.create_index('ix_document_chunks_workspace_doc', 'document_chunks', ['workspace_id', 'document_id'])
    op.create_index('ix_document_chunks_tsv', 'document_chunks', ['tsv'], postgresql_using='gin')
    op.create_index(
        'ix_document_chunks_embedding',
        'document_chunks',
        ['embedding'],
        postgresql_using='hnsw',
        postgresql_with={'m': 16, 'ef_construction': 64},
        postgresql_ops={'embedding': 'vector_cosine_ops'},
    )


def downgrade() -> None:
    # Drop in reverse order to respect foreign key constraints
    op.drop_index('ix_document_chunks_embedding', table_name='document_chunks')
    op.drop_index('ix_document_chunks_tsv', table_name='document_chunks')
    op.drop_index('ix_document_chunks_workspace_doc', table_name='document_chunks')
    op.drop_index('ix_document_chunks_doc_chunk', table_name='document_chunks')
    op.drop_index('ix_document_chunks_workspace_id', table_name='document_chunks')
    op.drop_index('ix_document_chunks_document_id', table_name='document_chunks')
    op.drop_table('document_chunks')

    op.drop_index('ix_document_files_workspace_created', table_name='document_files')
    op.drop_index('ix_document_files_workspace_id', table_name='document_files')
    op.drop_table('document_files')

    op.drop_constraint('fk_knowledge_items_current_revision_id', 'knowledge_items', type_='foreignkey')
    op.drop_index('ix_knowledge_revisions_embedding', table_name='knowledge_revisions')
    op.drop_index('ix_knowledge_revisions_item_version', table_name='knowledge_revisions')
    op.drop_index('ix_knowledge_revisions_item_id', table_name='knowledge_revisions')
    op.drop_table('knowledge_revisions')

    op.drop_index('ix_knowledge_items_global_deleted', table_name='knowledge_items')
    op.drop_index('ix_knowledge_items_workspace_deleted', table_name='knowledge_items')
    op.drop_index('ix_knowledge_items_workspace_id', table_name='knowledge_items')
    op.drop_table('knowledge_items')

    op.drop_table('workspaces')
    op.execute("DROP EXTENSION IF EXISTS vector;")
