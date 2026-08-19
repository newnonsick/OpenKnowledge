

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.gateway.config import settings

class Base(DeclarativeBase):

    pass

class Workspace(Base):

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    knowledge_items: Mapped[list["KnowledgeItem"]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    document_files: Mapped[list["DocumentFile"]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    document_chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
    )

class KnowledgeItem(Base):

    __tablename__ = "knowledge_items"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    current_revision_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "knowledge_revisions.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_knowledge_items_current_revision_id",
        ),
        nullable=True,
    )
    is_global: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="knowledge_items")
    revisions: Mapped[list["KnowledgeRevision"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        foreign_keys="[KnowledgeRevision.item_id]",
    )
    current_revision: Mapped[Optional["KnowledgeRevision"]] = relationship(
        foreign_keys=[current_revision_id],
        post_update=True,
    )

    __table_args__ = (
        Index("ix_knowledge_items_workspace_deleted", "workspace_id", "is_deleted"),
        Index("ix_knowledge_items_global_deleted", "is_global", "is_deleted"),
    )

EMBED_DIM = getattr(settings.embedding, "dimension", 768) if hasattr(settings, "embedding") else 768

class KnowledgeRevision(Base):

    __tablename__ = "knowledge_revisions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_items.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(EMBED_DIM), nullable=True)
    author: Mapped[str] = mapped_column(String(255), default="system", server_default=text("'system'"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    item: Mapped["KnowledgeItem"] = relationship(
        back_populates="revisions",
        foreign_keys=[item_id],
    )

    __table_args__ = (
        Index("ix_knowledge_revisions_item_version", "item_id", "version", unique=True),
        Index(
            "ix_knowledge_revisions_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

class DocumentFile(Base):

    __tablename__ = "document_files"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    is_global: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    workspace: Mapped["Workspace"] = relationship(back_populates="document_files")
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_document_files_workspace_created", "workspace_id", "created_at"),
    )

class DocumentChunk(Base):

    __tablename__ = "document_chunks"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    is_global: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    embedding: Mapped[Optional[list[float]]] = mapped_column(Vector(EMBED_DIM), nullable=True)
    tsv: Mapped[Optional[str]] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', content)", persisted=True),
        nullable=True,
    )
    metadata_: Mapped[dict] = mapped_column(
        "metadata",
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        nullable=False,
    )

    document: Mapped["DocumentFile"] = relationship(back_populates="chunks")
    workspace: Mapped["Workspace"] = relationship(back_populates="document_chunks")

    __table_args__ = (
        Index("ix_document_chunks_doc_chunk", "document_id", "chunk_index"),
        Index("ix_document_chunks_workspace_doc", "workspace_id", "document_id"),
        Index("ix_document_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_document_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


from src.gateway.infrastructure.persistence import identity_models as identity_models
