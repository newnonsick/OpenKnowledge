from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.gateway.application.parsers.code_parser import CODE_EXTENSIONS
from src.gateway.application.parsers.text_parser import TEXT_EXTENSIONS
from src.gateway.application.services.authorization_service import AuthorizationService
from src.gateway.application.services.document_upload_service import DocumentUploadService
from src.gateway.application.services.git_command import (
    GitCommandError,
    git_branch_commit,
    git_changed_paths,
    git_file_bytes,
    git_head_commit,
    git_is_repository,
    normalize_repo_root,
)
from src.gateway.domain.authorization import Action
from src.gateway.domain.exceptions import (
    AuthorizationException,
    ConcurrencyConflictException,
    ItemNotFoundException,
    ResourceConflictException,
    ValidationException,
)
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.persistence.ingestion_models import (
    DocumentModel,
    DocumentRevisionChunkModel,
    DocumentRevisionModel,
    RetrievalUnitModel,
    SourceConnectorFileModel,
    SourceConnectorModel,
)
from src.gateway.infrastructure.persistence.principal_context import bind_principal, reset_principal


_CONNECTOR_JSON_EXTENSIONS = frozenset({".json", ".jsonl", ".ndjson", ".geojson"})
_CONNECTOR_SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | CODE_EXTENSIONS | _CONNECTOR_JSON_EXTENSIONS | frozenset({".pdf"})

_CONNECTOR_SECRET_BASENAMES = frozenset(
    {
        ".env",
        "id_rsa",
        "id_ed25519",
        "id_dsa",
        "id_ecdsa",
        ".npmrc",
        ".pypirc",
        ".git-credentials",
        "secrets.yaml",
        "secrets.yml",
        "secrets.json",
        "secrets.toml",
        "secrets.ini",
        "credentials.yaml",
        "credentials.yml",
        "credentials.json",
        "credentials.toml",
        "credentials.ini",
        "token.json",
        "service-account.json",
        "client_secret.json",
        "client-secrets.json",
        "google-credentials.json",
        "gcp-key.json",
    }
)

_CONNECTOR_SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".kdbx"})

_CONNECTOR_SECRET_STEMS = frozenset({"id_rsa", "id_ed25519", "id_dsa", "id_ecdsa"})

_CONNECTOR_SECRET_STEM_PREFIXES = ("firebase-adminsdk",)

_CONNECTOR_SECRET_STEM_SUBSTRINGS = (
    "firebase-adminsdk",
    "service-account",
    "service_account",
    "client-secret",
    "client_secret",
    "client-secrets",
    "client_secrets",
    "google-credentials",
    "google_credentials",
    "gcp-key",
    "gcp_key",
)

_CONNECTOR_SECRET_SAMPLE_BASENAMES = frozenset(
    {
        ".env.sample",
        ".env.example",
        ".env.template",
        ".env.test",
    }
)


def _is_connector_secret_path(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    if name in _CONNECTOR_SECRET_SAMPLE_BASENAMES:
        return False
    if name in _CONNECTOR_SECRET_BASENAMES:
        return True
    suffixes = PurePosixPath(name).suffixes
    if suffixes == [".pub"]:
        return False
    if ".env" in suffixes:
        return True
    stem = PurePosixPath(name).stem
    if stem in _CONNECTOR_SECRET_STEMS:
        return True
    if stem.startswith(_CONNECTOR_SECRET_STEM_PREFIXES):
        return True
    normalized_stem = re.sub(r"[-_.]+", "-", stem)
    for substring in _CONNECTOR_SECRET_STEM_SUBSTRINGS:
        normalized = re.sub(r"[-_.]+", "-", substring)
        if re.search(r"(?:^|-)" + re.escape(normalized) + r"(?:-|$)", normalized_stem):
            return True
    if suffixes and PurePosixPath(stem).name in _CONNECTOR_SECRET_BASENAMES:
        return True
    if any(suffix in _CONNECTOR_SECRET_SUFFIXES for suffix in suffixes):
        return True
    return name.startswith(".env.")

_CONNECTOR_MIME_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".geojson": "application/json",
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
}


@dataclass(frozen=True, slots=True)
class ConnectorSummary:
    id: UUID
    space_id: str
    kind: str
    repo_root: str
    branch: str
    last_synced_commit: str | None
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConnectorRegistration:
    connector_id: UUID
    replayed: bool


@dataclass(frozen=True, slots=True)
class ConnectorSyncResult:
    connector_id: UUID
    commit: str
    enqueued: int
    archived: int
    skipped: int
    skipped_reasons: tuple


def connector_payload(summary: ConnectorSummary) -> dict:
    return {
        "id": str(summary.id),
        "space_id": summary.space_id,
        "kind": summary.kind,
        "repo_root": summary.repo_root,
        "branch": summary.branch,
        "last_synced_commit": summary.last_synced_commit,
        "status": summary.status,
        "created_at": summary.created_at.isoformat(),
        "updated_at": summary.updated_at.isoformat(),
    }


def _actor_id(principal: Principal) -> UUID:
    try:
        return UUID(principal.subject_id)
    except ValueError as exc:
        raise AuthorizationException() from exc


def _mime_type_for(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in _CONNECTOR_MIME_TYPES:
        return _CONNECTOR_MIME_TYPES[suffix]
    if suffix in CODE_EXTENSIONS:
        if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
            return "application/javascript"
        if suffix in {".ts", ".tsx"}:
            return "application/typescript"
        if suffix in {".py", ".pyi"}:
            return "application/x-python-code"
        if suffix in {".sh", ".bash", ".zsh"}:
            return "application/x-sh"
        return "text/plain"
    return "text/plain"


async def _file_chunks(content: bytes):
    yield content


class GitConnectorService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        storage,
        *,
        max_upload_bytes: int,
        git_timeout_seconds: float = 30.0,
    ) -> None:
        if max_upload_bytes <= 0:
            raise ValueError("A positive upload limit is required")
        self._session_factory = session_factory
        self._storage = storage
        self._max_upload_bytes = max_upload_bytes
        self._git_timeout_seconds = git_timeout_seconds

    async def register(
        self,
        *,
        principal: Principal,
        space_id: str,
        repo: str,
        branch: str,
        idempotency_key: str,
    ) -> ConnectorRegistration:
        normalized_repo = _normalize_repo_input(repo)
        normalized_branch = branch.strip()
        if not normalized_branch or len(normalized_branch) > 255:
            raise ValidationException("A valid branch name is required.")
        _validate_idempotency_key(idempotency_key)
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                await AuthorizationService(session).authorize_space(principal, space_id, Action.CONTENT_WRITE)
                try:
                    await git_branch_commit(normalized_repo, normalized_branch, timeout_seconds=self._git_timeout_seconds)
                except GitCommandError as exc:
                    raise ValidationException(str(exc)) from exc
                connector_id = uuid4()
                try:
                    async with session.begin_nested():
                        session.add(
                            SourceConnectorModel(
                                id=connector_id,
                                space_id=space_id,
                                kind="git",
                                repo_root=normalized_repo,
                                branch=normalized_branch,
                                status="active",
                                created_by_member_id=_actor_id(principal),
                            )
                        )
                        await session.flush()
                except IntegrityError as exc:
                    existing_id = await session.scalar(
                        select(SourceConnectorModel.id).where(
                            SourceConnectorModel.space_id == space_id,
                            SourceConnectorModel.repo_root == normalized_repo,
                            SourceConnectorModel.branch == normalized_branch,
                        )
                    )
                    if existing_id is None:
                        raise ResourceConflictException("The git connector is already registered.") from exc
                    connector_id = existing_id
                return ConnectorRegistration(connector_id=connector_id, replayed=False)
        finally:
            reset_principal(token)

    async def registration_replay(self, *, principal: Principal, space_id: str, resource_ids: tuple[str, ...]):
        if not resource_ids:
            raise ResourceConflictException("The connector registration is unavailable.")
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                await AuthorizationService(session).authorize_space(principal, space_id, Action.CONTENT_WRITE)
                connector = await session.get(SourceConnectorModel, UUID(resource_ids[0]))
                if connector is None or connector.space_id != space_id:
                    raise ResourceConflictException("The connector registration is unavailable.")
                return ConnectorRegistration(connector_id=connector.id, replayed=True)
        finally:
            reset_principal(token)

    async def list(self, *, principal: Principal, space_id: str | None) -> list[ConnectorSummary]:
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                effective = await AuthorizationService(session).effective_space_ids(
                    _actor_id(principal), principal=principal
                )
                if space_id is not None:
                    if space_id not in effective:
                        return []
                    scoped = (space_id,)
                else:
                    scoped = effective
                    if not scoped:
                        return []
                rows = list(
                    await session.scalars(
                        select(SourceConnectorModel)
                        .where(
                            SourceConnectorModel.space_id.in_(scoped),
                            SourceConnectorModel.status == "active",
                        )
                        .order_by(SourceConnectorModel.created_at.desc(), SourceConnectorModel.id.desc())
                    )
                )
                return [
                    ConnectorSummary(
                        id=row.id,
                        space_id=row.space_id,
                        kind=row.kind,
                        repo_root=row.repo_root,
                        branch=row.branch,
                        last_synced_commit=row.last_synced_commit,
                        status=row.status,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                    )
                    for row in rows
                ]
        finally:
            reset_principal(token)

    async def unregister(self, *, principal: Principal, connector_id: UUID, request_id: str) -> None:
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                connector = await session.get(SourceConnectorModel, connector_id)
                if connector is None or connector.status != "active":
                    raise ItemNotFoundException("The git connector was not found.")
                await AuthorizationService(session).authorize_space(
                    principal, connector.space_id, Action.CONTENT_WRITE
                )
                connector.status = "deleted"
                connector.updated_at = datetime.now(timezone.utc)
                await session.flush()
        finally:
            reset_principal(token)

    async def sync(
        self,
        *,
        principal: Principal,
        connector_id: UUID,
        idempotency_key: str,
    ) -> ConnectorSyncResult:
        _validate_idempotency_key(idempotency_key)
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                connector = await session.get(SourceConnectorModel, connector_id)
                if connector is None or connector.status != "active":
                    raise ItemNotFoundException("The git connector was not found.")
                await AuthorizationService(session).authorize_space(
                    principal, connector.space_id, Action.CONTENT_WRITE
                )
                space_id = connector.space_id
                repo_root = connector.repo_root
                branch = connector.branch
                base_commit = connector.last_synced_commit
        finally:
            reset_principal(token)
        try:
            head_commit = await git_head_commit(repo_root, branch, timeout_seconds=self._git_timeout_seconds)
        except GitCommandError as exc:
            raise ValidationException(str(exc)) from exc
        if base_commit == head_commit and base_commit is not None:
            return ConnectorSyncResult(
                connector_id=connector_id,
                commit=head_commit,
                enqueued=0,
                archived=0,
                skipped=0,
                skipped_reasons=(),
            )
        try:
            changes = await git_changed_paths(
                repo_root, base_commit, head_commit, timeout_seconds=self._git_timeout_seconds
            )
        except GitCommandError as exc:
            raise ValidationException(str(exc)) from exc
        enqueued = 0
        archived = 0
        skipped: list = []
        for change in changes:
            outcome = await self._sync_path(
                principal=principal,
                connector_id=connector_id,
                space_id=space_id,
                repo_root=repo_root,
                head_commit=head_commit,
                change=change,
            )
            if outcome == "enqueued":
                enqueued += 1
            elif outcome == "archived":
                archived += 1
            else:
                skipped.append(outcome)
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                result = await session.execute(
                    update(SourceConnectorModel)
                    .where(
                        SourceConnectorModel.id == connector_id,
                        SourceConnectorModel.status == "active",
                        SourceConnectorModel.space_id == space_id,
                    )
                    .values(last_synced_commit=head_commit, updated_at=datetime.now(timezone.utc))
                    .returning(SourceConnectorModel.id)
                )
                if result.scalar_one_or_none() is None:
                    raise ConcurrencyConflictException("The git connector changed before sync completed.")
        finally:
            reset_principal(token)
        return ConnectorSyncResult(
            connector_id=connector_id,
            commit=head_commit,
            enqueued=enqueued,
            archived=archived,
            skipped=len(skipped),
            skipped_reasons=tuple(skipped),
        )

    async def _sync_path(
        self,
        *,
        principal: Principal,
        connector_id: UUID,
        space_id: str,
        repo_root: str,
        head_commit: str,
        change,
    ) -> str:
        suffix = PurePosixPath(change.path).suffix.lower()
        if change.change_type == "deleted":
            await self._archive_connector_document(principal, connector_id, space_id, change.path)
            return "archived"
        if _is_connector_secret_path(change.path):
            return f"skipped:secret-file:{change.path}"
        if suffix not in _CONNECTOR_SUPPORTED_EXTENSIONS:
            return f"skipped:unsupported-extension:{change.path}"
        try:
            content = await git_file_bytes(
                repo_root,
                head_commit,
                change.path,
                max_bytes=self._max_upload_bytes,
                timeout_seconds=self._git_timeout_seconds,
            )
        except GitCommandError as exc:
            return f"skipped:{exc.code}:{change.path}"
        checksum = hashlib.sha256(content).hexdigest()
        existing = await self._existing_mapping(connector_id, change.path)
        if existing is not None and existing[1] == checksum:
            return f"skipped:unchanged:{change.path}"
        filename = PurePosixPath(change.path).name
        mime_type = _mime_type_for(filename)
        uploader = DocumentUploadService(
            self._session_factory,
            self._storage,
            max_upload_bytes=self._max_upload_bytes,
        )
        try:
            if existing is None:
                receipt = await uploader.upload_new(
                    principal=principal,
                    space_id=space_id,
                    display_name=change.path,
                    original_filename=filename,
                    mime_type=mime_type,
                    chunks=_file_chunks(content),
                    idempotency_key=f"git:{connector_id}:{head_commit}:{checksum}",
                )
                await self._record_mapping(connector_id, space_id, change.path, receipt.document_id, checksum)
            else:
                document_id, _ = existing
                revision = await self._active_document_revision(principal, space_id, document_id)
                if revision is None:
                    if await self._pending_document_revision(principal, space_id, document_id):
                        return f"skipped:pending:{change.path}"
                    receipt = await uploader.upload_new(
                        principal=principal,
                        space_id=space_id,
                        display_name=change.path,
                        original_filename=filename,
                        mime_type=mime_type,
                        chunks=_file_chunks(content),
                        idempotency_key=f"git:{connector_id}:{head_commit}:{checksum}",
                    )
                    await self._record_mapping(connector_id, space_id, change.path, receipt.document_id, checksum)
                else:
                    receipt = await uploader.upload_revision(
                        principal=principal,
                        document_id=document_id,
                        expected_revision=revision,
                        original_filename=filename,
                        mime_type=mime_type,
                        chunks=_file_chunks(content),
                        idempotency_key=f"git:{connector_id}:{head_commit}:{checksum}",
                    )
                    await self._update_mapping_checksum(connector_id, change.path, checksum)
        except ValidationException as exc:
            return f"skipped:rejected:{change.path}:{exc.message}"
        return "enqueued"

    async def _existing_mapping(self, connector_id: UUID, repo_path: str) -> tuple | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        SourceConnectorFileModel.document_id,
                        SourceConnectorFileModel.last_synced_checksum,
                    ).where(
                        SourceConnectorFileModel.connector_id == connector_id,
                        SourceConnectorFileModel.repo_path == repo_path,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            return (row.document_id, row.last_synced_checksum)

    async def _record_mapping(
        self,
        connector_id: UUID,
        space_id: str,
        repo_path: str,
        document_id: UUID,
        checksum: str,
    ) -> None:
        async with self._session_factory.begin() as session:
            existing = await session.scalar(
                select(SourceConnectorFileModel).where(
                    SourceConnectorFileModel.connector_id == connector_id,
                    SourceConnectorFileModel.repo_path == repo_path,
                )
            )
            if existing is not None:
                existing.document_id = document_id
                existing.last_synced_checksum = checksum
                existing.updated_at = datetime.now(timezone.utc)
            else:
                session.add(
                    SourceConnectorFileModel(
                        connector_id=connector_id,
                        space_id=space_id,
                        repo_path=repo_path,
                        document_id=document_id,
                        last_synced_checksum=checksum,
                    )
                )

    async def _update_mapping_checksum(self, connector_id: UUID, repo_path: str, checksum: str) -> None:
        async with self._session_factory.begin() as session:
            await session.execute(
                update(SourceConnectorFileModel)
                .where(
                    SourceConnectorFileModel.connector_id == connector_id,
                    SourceConnectorFileModel.repo_path == repo_path,
                )
                .values(last_synced_checksum=checksum, updated_at=datetime.now(timezone.utc))
            )

    async def _active_document_revision(
        self, principal: Principal, space_id: str, document_id: UUID
    ) -> int | None:
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                await AuthorizationService(session).authorize_space(principal, space_id, Action.CONTENT_WRITE)
                document = await session.scalar(
                    select(DocumentModel).where(
                        DocumentModel.id == document_id,
                        DocumentModel.space_id == space_id,
                        DocumentModel.archived_at.is_(None),
                    )
                )
                if document is None or document.current_revision_id is None:
                    return None
                return int(document.revision)
        finally:
            reset_principal(token)

    async def _pending_document_revision(self, principal: Principal, space_id: str, document_id: UUID) -> bool:
        token = bind_principal(principal)
        try:
            async with self._session_factory() as session:
                await AuthorizationService(session).authorize_space(principal, space_id, Action.CONTENT_WRITE)
                pending = await session.scalar(
                    select(DocumentRevisionModel.id).where(
                        DocumentRevisionModel.document_id == document_id,
                        DocumentRevisionModel.space_id == space_id,
                        DocumentRevisionModel.status.in_(("pending", "processing")),
                    )
                )
                return pending is not None
        finally:
            reset_principal(token)

    async def _archive_connector_document(
        self, principal: Principal, connector_id: UUID, space_id: str, repo_path: str
    ) -> None:
        token = bind_principal(principal)
        try:
            async with self._session_factory.begin() as session:
                await AuthorizationService(session).authorize_space(principal, space_id, Action.CONTENT_WRITE)
                mapping = await session.scalar(
                    select(SourceConnectorFileModel).where(
                        SourceConnectorFileModel.connector_id == connector_id,
                        SourceConnectorFileModel.repo_path == repo_path,
                    )
                )
                if mapping is None:
                    return
                document = await session.scalar(
                    select(DocumentModel).where(
                        DocumentModel.id == mapping.document_id,
                        DocumentModel.space_id == space_id,
                        DocumentModel.archived_at.is_(None),
                    )
                )
                if document is None:
                    await session.delete(mapping)
                    return
                current_time = datetime.now(timezone.utc)
                document.archived_at = current_time
                document.revision += 1
                document.updated_at = current_time
                chunk_ids = select(DocumentRevisionChunkModel.id).where(
                    DocumentRevisionChunkModel.document_id == document.id
                )
                await session.execute(
                    update(RetrievalUnitModel)
                    .where(
                        RetrievalUnitModel.document_revision_chunk_id.in_(chunk_ids),
                        RetrievalUnitModel.active.is_(True),
                    )
                    .values(active=False, deactivated_at=current_time)
                    .execution_options(synchronize_session=False)
                )
                await session.delete(mapping)
        finally:
            reset_principal(token)


def _normalize_repo_input(repo: str) -> str:
    candidate = repo.strip()
    if not candidate:
        raise ValidationException("A valid repository path or file URL is required.")
    if len(candidate) > 2000:
        raise ValidationException("A valid repository path or file URL is required.")
    try:
        normalized = normalize_repo_root(candidate)
    except GitCommandError as exc:
        raise ValidationException(str(exc)) from exc
    if not git_is_repository(normalized):
        raise ValidationException("The repository path is not a git repository.")
    return normalized


def _validate_idempotency_key(idempotency_key: str) -> None:
    if not idempotency_key.strip() or len(idempotency_key) > 128:
        raise ValidationException("A valid idempotency key is required.")


def sync_result_payload(result: ConnectorSyncResult) -> dict:
    return {
        "connector_id": str(result.connector_id),
        "commit": result.commit,
        "enqueued": result.enqueued,
        "archived": result.archived,
        "skipped": result.skipped,
        "skipped_reasons": list(result.skipped_reasons),
    }
