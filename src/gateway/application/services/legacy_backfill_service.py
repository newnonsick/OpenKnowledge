from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.gateway.application.ports.object_storage import IVersionedObjectStorage
from src.gateway.application.ports.storage import IFileStorage
from src.gateway.domain.exceptions import ConcurrencyConflictException, ValidationException
from src.gateway.infrastructure.persistence.identity_models import MemberModel
from src.gateway.infrastructure.persistence.ingestion_models import DocumentModel, DocumentRevisionChunkModel, DocumentRevisionModel, EmbeddingGenerationModel, MigrationBackfillRunModel, ProvenanceLinkModel, RetrievalUnitModel
from src.gateway.infrastructure.persistence.models import DocumentChunk, DocumentFile, KnowledgeRevision


@dataclass(frozen=True, slots=True)
class BackfillStatus:
    name: str
    phase: str
    high_water_created_at: datetime | None
    high_water_id: UUID | None
    rows_migrated: int


class LegacyBackfillService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        legacy_storage: IFileStorage,
        versioned_storage: IVersionedObjectStorage,
        *,
        migration_actor_id: UUID,
    ) -> None:
        self._session_factory = session_factory
        self._legacy_storage = legacy_storage
        self._versioned_storage = versioned_storage
        self._migration_actor_id = migration_actor_id

    async def start_document_snapshot(self) -> BackfillStatus:
        async with self._session_factory.begin() as session:
            await self._require_actor(session)
            run = await session.get(MigrationBackfillRunModel, "legacy_documents")
            if run is None:
                latest = (
                    await session.execute(
                        select(DocumentFile.created_at, DocumentFile.id)
                        .order_by(DocumentFile.created_at.desc(), DocumentFile.id.desc())
                        .limit(1)
                    )
                ).one_or_none()
                run = MigrationBackfillRunModel(
                    name="legacy_documents",
                    phase="snapshot",
                    high_water_created_at=latest.created_at if latest else None,
                    high_water_id=latest.id if latest else None,
                )
                session.add(run)
                await session.flush()
            return self._status(run)

    async def run_document_snapshot_batch(self, *, batch_size: int) -> int:
        self._validate_batch_size(batch_size)
        await self.start_document_snapshot()
        async with self._session_factory() as session:
            run = await session.get(MigrationBackfillRunModel, "legacy_documents")
            if run.phase == "complete":
                return 0
            if run.phase != "snapshot":
                raise ConcurrencyConflictException("The document snapshot phase is no longer active.")
            rows = await self._document_batch(
                session,
                lower_created_at=run.cursor_created_at,
                lower_id=run.cursor_id,
                upper_created_at=run.high_water_created_at,
                upper_id=run.high_water_id,
                batch_size=batch_size,
            )
        if not rows:
            await self._start_delta()
            return 0
        migrated = 0
        for document in rows:
            inserted = await self._migrate_document(document.id)
            migrated += int(inserted)
            await self._advance(
                "legacy_documents",
                document.created_at,
                document.id,
                migrated=int(inserted),
            )
        return migrated

    async def run_document_delta_batch(self, *, batch_size: int) -> int:
        self._validate_batch_size(batch_size)
        async with self._session_factory() as session:
            run = await session.get(MigrationBackfillRunModel, "legacy_documents")
            if run is None or run.phase == "snapshot":
                raise ConcurrencyConflictException("The document snapshot phase must finish first.")
            if run.phase == "complete":
                return 0
            if run.phase != "delta":
                raise ConcurrencyConflictException("The document delta phase is unavailable.")
            rows = await self._document_batch(
                session,
                lower_created_at=run.cursor_created_at,
                lower_id=run.cursor_id,
                upper_created_at=run.delta_high_water_created_at,
                upper_id=run.delta_high_water_id,
                batch_size=batch_size,
            )
        if not rows:
            await self._complete("legacy_documents")
            return 0
        migrated = 0
        for document in rows:
            inserted = await self._migrate_document(document.id)
            migrated += int(inserted)
            await self._advance(
                "legacy_documents",
                document.created_at,
                document.id,
                migrated=int(inserted),
            )
        return migrated

    async def run_knowledge_provenance_batch(self, *, batch_size: int) -> int:
        self._validate_batch_size(batch_size)
        await self._start_knowledge_snapshot()
        async with self._session_factory() as session:
            run = await session.get(MigrationBackfillRunModel, "legacy_knowledge_provenance")
            if run.phase == "complete":
                return 0
            if run.high_water_created_at is None or run.high_water_id is None:
                rows = ()
            else:
                bounds = self._bounds(
                    KnowledgeRevision.created_at,
                    KnowledgeRevision.id,
                    run.cursor_created_at,
                    run.cursor_id,
                    run.high_water_created_at,
                    run.high_water_id,
                )
                rows = tuple(
                    await session.scalars(
                        select(KnowledgeRevision)
                        .where(*bounds)
                        .order_by(KnowledgeRevision.created_at, KnowledgeRevision.id)
                        .limit(batch_size)
                    )
                )
        if not rows:
            await self._complete("legacy_knowledge_provenance")
            return 0
        migrated = 0
        for revision in rows:
            async with self._session_factory.begin() as session:
                exists = await session.scalar(
                    select(func.count()).select_from(ProvenanceLinkModel).where(
                        ProvenanceLinkModel.knowledge_revision_id == revision.id
                    )
                )
                if not exists:
                    session.add(
                        ProvenanceLinkModel(
                            space_id=revision.space_id,
                            knowledge_revision_id=revision.id,
                            source_type="manual",
                            actor_member_id=self._migration_actor_id,
                            source_metadata={"migration": "legacy_knowledge"},
                        )
                    )
                    migrated += 1
            await self._advance(
                "legacy_knowledge_provenance",
                revision.created_at,
                revision.id,
                migrated=int(not exists),
            )
        return migrated

    async def _start_delta(self) -> None:
        async with self._session_factory.begin() as session:
            run = await session.scalar(
                select(MigrationBackfillRunModel)
                .where(MigrationBackfillRunModel.name == "legacy_documents")
                .with_for_update()
            )
            if run is None or run.phase != "snapshot":
                return
            latest = (
                await session.execute(
                    select(DocumentFile.created_at, DocumentFile.id)
                    .order_by(DocumentFile.created_at.desc(), DocumentFile.id.desc())
                    .limit(1)
                )
            ).one_or_none()
            run.phase = "delta"
            run.delta_high_water_created_at = latest.created_at if latest else None
            run.delta_high_water_id = latest.id if latest else None
            run.cursor_created_at = run.high_water_created_at
            run.cursor_id = run.high_water_id
            run.updated_at = await session.scalar(select(func.now()))

    async def _start_knowledge_snapshot(self) -> None:
        async with self._session_factory.begin() as session:
            await self._require_actor(session)
            run = await session.get(MigrationBackfillRunModel, "legacy_knowledge_provenance")
            if run is not None:
                return
            latest = (
                await session.execute(
                    select(KnowledgeRevision.created_at, KnowledgeRevision.id)
                    .order_by(KnowledgeRevision.created_at.desc(), KnowledgeRevision.id.desc())
                    .limit(1)
                )
            ).one_or_none()
            session.add(
                MigrationBackfillRunModel(
                    name="legacy_knowledge_provenance",
                    phase="snapshot",
                    high_water_created_at=latest.created_at if latest else None,
                    high_water_id=latest.id if latest else None,
                )
            )

    async def _migrate_document(self, document_id: UUID) -> bool:
        async with self._session_factory() as session:
            if await session.get(DocumentModel, document_id) is not None:
                return False
            legacy = await session.get(DocumentFile, document_id)
            if legacy is None:
                raise ValidationException("A legacy document disappeared during backfill.")
            space_id = legacy.workspace_id
            filename = legacy.filename
            expected_size = legacy.file_size
        content = await self._legacy_storage.read_file(space_id, document_id, filename)
        if len(content) != expected_size:
            raise ValidationException("A legacy document size does not match its database record.")
        checksum = hashlib.sha256(content).hexdigest()
        revision_id = uuid5(NAMESPACE_URL, f"openknowledge:legacy-document:{document_id}:1")
        final_key = f"objects/{space_id}/{document_id}/{revision_id}"
        if await self._versioned_storage.exists(final_key):
            existing = await self._versioned_storage.read(final_key)
            if existing != content:
                raise ValidationException("An immutable backfill object has conflicting content.")
        else:
            staging_key = f"staging/{space_id}/{revision_id}"
            if await self._versioned_storage.exists(staging_key):
                staged_content = await self._versioned_storage.read(staging_key)
                if staged_content != content:
                    await self._versioned_storage.delete(staging_key)
                    staged = await self._stage(space_id, revision_id, content)
                    staging_key = staged.storage_key
            else:
                staged = await self._stage(space_id, revision_id, content)
                staging_key = staged.storage_key
            final_key = await self._versioned_storage.finalize(
                staging_key,
                space_id=space_id,
                document_id=document_id,
                revision_id=revision_id,
            )
        async with self._session_factory.begin() as session:
            await self._require_actor(session)
            if await session.get(DocumentModel, document_id) is not None:
                return False
            legacy = await session.get(DocumentFile, document_id)
            if legacy is None:
                raise ValidationException("A legacy document disappeared during backfill.")
            chunks = tuple(
                await session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_id == document_id)
                    .order_by(DocumentChunk.chunk_index, DocumentChunk.id)
                )
            )
            generation = await session.scalar(
                select(EmbeddingGenerationModel).where(
                    EmbeddingGenerationModel.purpose == "retrieval",
                    EmbeddingGenerationModel.status == "active",
                )
            )
            projection_ready = bool(chunks) and generation is not None and all(
                chunk.embedding is not None and len(chunk.embedding) == generation.dimensions
                for chunk in chunks
            )
            now = await session.scalar(select(func.now()))
            document = DocumentModel(
                id=document_id,
                space_id=legacy.workspace_id,
                display_name=legacy.filename,
                created_by_member_id=self._migration_actor_id,
                created_at=legacy.created_at,
                updated_at=legacy.created_at,
            )
            session.add(document)
            await session.flush()
            revision = DocumentRevisionModel(
                id=revision_id,
                document_id=document_id,
                space_id=legacy.workspace_id,
                version=1,
                original_filename=legacy.filename,
                mime_type=legacy.mime_type,
                size_bytes=len(content),
                checksum_sha256=checksum,
                storage_key=final_key,
                parser_version="legacy-v1",
                status="active" if projection_ready else "ready",
                created_by_member_id=self._migration_actor_id,
                created_at=legacy.created_at,
                ready_at=now,
                activated_at=now if projection_ready else None,
            )
            session.add(revision)
            await session.flush()
            for chunk in chunks:
                content_hash = (chunk.metadata_ or {}).get("content_hash") or hashlib.sha256(
                    chunk.content.encode("utf-8")
                ).hexdigest()
                session.add(
                    DocumentRevisionChunkModel(
                        id=chunk.id,
                        document_revision_id=revision_id,
                        document_id=document_id,
                        space_id=legacy.workspace_id,
                        chunk_index=chunk.chunk_index,
                        content=chunk.content,
                        content_hash=content_hash,
                        parser_metadata={**(chunk.metadata_ or {}), "migration": "legacy_document"},
                        created_at=legacy.created_at,
                    )
                )
            await session.flush()
            if projection_ready:
                for chunk in chunks:
                    session.add(
                        RetrievalUnitModel(
                            id=uuid5(NAMESPACE_URL, f"openknowledge:legacy-retrieval:{chunk.id}:{generation.id}"),
                            space_id=legacy.workspace_id,
                            source_type="document_chunk",
                            document_revision_chunk_id=chunk.id,
                            embedding_generation_id=generation.id,
                            title=legacy.filename,
                            content=chunk.content,
                            source_metadata={
                                "document_id": str(document_id),
                                "document_revision_id": str(revision_id),
                                "chunk_index": chunk.chunk_index,
                                "source_filename": legacy.filename,
                                "parser_version": "legacy-v1",
                                "migration": "legacy_document",
                            },
                            embedding=list(chunk.embedding),
                            active=True,
                        )
                    )
            document.current_revision_id = revision_id
            await session.flush()
            return True

    async def _stage(self, space_id: str, revision_id: UUID, content: bytes):
        async def chunks():
            yield content

        return await self._versioned_storage.stage(
            space_id=space_id,
            upload_id=revision_id,
            chunks=chunks(),
            max_bytes=max(len(content), 1),
        )

    async def _document_batch(
        self,
        session,
        *,
        lower_created_at,
        lower_id,
        upper_created_at,
        upper_id,
        batch_size: int,
    ):
        if upper_created_at is None or upper_id is None:
            return ()
        bounds = self._bounds(
            DocumentFile.created_at,
            DocumentFile.id,
            lower_created_at,
            lower_id,
            upper_created_at,
            upper_id,
        )
        return tuple(
            await session.scalars(
                select(DocumentFile)
                .where(*bounds)
                .order_by(DocumentFile.created_at, DocumentFile.id)
                .limit(batch_size)
            )
        )

    @staticmethod
    def _bounds(created_column, id_column, lower_created_at, lower_id, upper_created_at, upper_id):
        conditions = [
            or_(
                created_column < upper_created_at,
                and_(created_column == upper_created_at, id_column <= upper_id),
            )
        ]
        if lower_created_at is not None and lower_id is not None:
            conditions.append(
                or_(
                    created_column > lower_created_at,
                    and_(created_column == lower_created_at, id_column > lower_id),
                )
            )
        return tuple(conditions)

    async def _advance(
        self,
        name: str,
        created_at: datetime,
        resource_id: UUID,
        *,
        migrated: int,
    ) -> None:
        async with self._session_factory.begin() as session:
            run = await session.scalar(
                select(MigrationBackfillRunModel)
                .where(MigrationBackfillRunModel.name == name)
                .with_for_update()
            )
            run.cursor_created_at = created_at
            run.cursor_id = resource_id
            run.rows_migrated += migrated
            run.updated_at = await session.scalar(select(func.now()))

    async def _complete(self, name: str) -> None:
        async with self._session_factory.begin() as session:
            run = await session.scalar(
                select(MigrationBackfillRunModel)
                .where(MigrationBackfillRunModel.name == name)
                .with_for_update()
            )
            if run is None or run.phase == "complete":
                return
            now = await session.scalar(select(func.now()))
            run.phase = "complete"
            run.completed_at = now
            run.updated_at = now

    async def _require_actor(self, session) -> None:
        actor = await session.get(MemberModel, self._migration_actor_id)
        if actor is None or actor.status not in {"active", "disabled"}:
            raise ValidationException("The explicit migration actor is unavailable.")

    @staticmethod
    def _validate_batch_size(batch_size: int) -> None:
        if batch_size <= 0 or batch_size > 1000:
            raise ValueError("Backfill batch size must be between 1 and 1000")

    @staticmethod
    def _status(run: MigrationBackfillRunModel) -> BackfillStatus:
        return BackfillStatus(
            name=run.name,
            phase=run.phase,
            high_water_created_at=run.high_water_created_at,
            high_water_id=run.high_water_id,
            rows_migrated=run.rows_migrated,
        )
