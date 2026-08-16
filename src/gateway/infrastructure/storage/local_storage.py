

import asyncio
import os
import random
import re
import shutil
import time
from pathlib import Path
from typing import BinaryIO, Union
from uuid import UUID

from src.gateway.application.ports.storage import IFileStorage
from src.gateway.domain.exceptions import ItemNotFoundException, StorageException

SAFE_ID_PATTERN = re.compile(r"\A[a-zA-Z0-9_-]+\Z")

class LocalStorageAdapter(IFileStorage):

    def __init__(self, base_dir: Union[str, Path] = "./data/storage") -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _sanitize_filename(self, filename: str) -> str:

        if not filename or not str(filename).strip():
            return "unnamed_file"

        clean_name = str(filename).replace("\x00", "")
        clean_name = Path(clean_name).name
        clean_name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', '_', clean_name)
        clean_name = clean_name.strip(". \t\r\n")

        if not clean_name:
            return "unnamed_file"
        return clean_name

    def _validate_workspace_id(self, workspace_id: str) -> str:

        ws_id = str(workspace_id)
        if not ws_id or not SAFE_ID_PATTERN.match(ws_id):
            raise StorageException(
                f"Invalid workspace_id: '{workspace_id}'. Only alphanumeric, hyphens, and underscores are allowed."
            )
        return ws_id

    def _validate_file_id(self, file_id: Union[str, UUID]) -> str:

        f_id = str(file_id)
        if not f_id or not SAFE_ID_PATTERN.match(f_id):
            raise StorageException(
                f"Invalid file_id: '{file_id}'. Only alphanumeric, hyphens, and underscores are allowed."
            )
        return f_id

    @staticmethod
    def _is_contained_in(child: Path, parent: Path) -> bool:

        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except ValueError:
            child_str = os.path.abspath(str(child))
            parent_str = os.path.abspath(str(parent))
            if child_str.startswith("\\\\?\\"):
                child_str = child_str[4:]
            if parent_str.startswith("\\\\?\\"):
                parent_str = parent_str[4:]
            try:
                return os.path.commonpath([child_str, parent_str]) == parent_str
            except (ValueError, OSError):
                return False

    def _resolve_file_path(self, workspace_id: str, file_id: Union[str, UUID], filename: str) -> Path:

        ws_id = self._validate_workspace_id(workspace_id)
        f_id = self._validate_file_id(file_id)
        safe_filename = self._sanitize_filename(filename)

        ws_dir = (self.base_dir / ws_id).resolve()
        if not self._is_contained_in(ws_dir, self.base_dir):
            raise StorageException(f"Path traversal detected: workspace directory '{ws_id}' escapes base storage directory.")

        target_file = (ws_dir / f"{f_id}_{safe_filename}").resolve()
        if not self._is_contained_in(target_file, ws_dir):
            raise StorageException("Path traversal detected: file path escapes workspace directory.")

        return target_file

    def _resolve_workspace_dir(self, workspace_id: str) -> Path:

        ws_id = self._validate_workspace_id(workspace_id)
        ws_dir = (self.base_dir / ws_id).resolve()
        if not self._is_contained_in(ws_dir, self.base_dir):
            raise StorageException(f"Path traversal detected: workspace directory '{ws_id}' escapes base storage directory.")
        return ws_dir

    def _sync_save(self, target_path: Path, content: Union[bytes, BinaryIO]) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target_path.parent / f".tmp_{os.urandom(8).hex()}"
        try:
            with open(temp_path, "wb") as f:
                if isinstance(content, bytes):
                    f.write(content)
                else:
                    shutil.copyfileobj(content, f)

            max_retries = 10
            for attempt in range(max_retries):
                try:
                    temp_path.replace(target_path)
                    break
                except (PermissionError, OSError):
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(0.005 * (2 ** attempt) + random.uniform(0.001, 0.005))
            return target_path
        except Exception as e:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise StorageException(f"Failed to save file '{target_path.name}': {str(e)}") from e

    def _sync_read(self, target_path: Path) -> bytes:
        if not target_path.exists() or not target_path.is_file():
            raise ItemNotFoundException(f"File not found: {target_path.name}")

        max_retries = 10
        for attempt in range(max_retries):
            try:
                with open(target_path, "rb") as f:
                    return f.read()
            except (ItemNotFoundException, FileNotFoundError):
                raise ItemNotFoundException(f"File not found: {target_path.name}")
            except (PermissionError, OSError) as err:
                if attempt == max_retries - 1:
                    raise StorageException(f"Failed to read file '{target_path.name}': {str(err)}") from err
                time.sleep(0.005 * (2 ** attempt) + random.uniform(0.001, 0.005))
            except Exception as e:
                raise StorageException(f"Failed to read file '{target_path.name}': {str(e)}") from e
        raise ItemNotFoundException(f"File not found: {target_path.name}")

    def _sync_delete(self, target_path: Path) -> bool:
        if not target_path.exists():
            return False
        try:
            target_path.unlink()
            return True
        except Exception as e:
            raise StorageException(f"Failed to delete file '{target_path.name}': {str(e)}") from e

    def _sync_delete_workspace(self, ws_dir: Path) -> bool:
        if not ws_dir.exists():
            return False
        try:
            shutil.rmtree(ws_dir)
            return True
        except Exception as e:
            raise StorageException(f"Failed to delete workspace directory '{ws_dir.name}': {str(e)}") from e

    async def save_file(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
        content: Union[bytes, BinaryIO],
    ) -> Path:
        target_path = self._resolve_file_path(workspace_id, file_id, filename)
        return await asyncio.to_thread(self._sync_save, target_path, content)

    async def read_file(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
    ) -> bytes:
        target_path = self._resolve_file_path(workspace_id, file_id, filename)
        return await asyncio.to_thread(self._sync_read, target_path)

    async def delete_file(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
    ) -> bool:
        target_path = self._resolve_file_path(workspace_id, file_id, filename)
        return await asyncio.to_thread(self._sync_delete, target_path)

    async def exists(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
    ) -> bool:
        try:
            target_path = self._resolve_file_path(workspace_id, file_id, filename)
            return await asyncio.to_thread(lambda: target_path.exists() and target_path.is_file())
        except StorageException:
            return False

    async def file_exists(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
    ) -> bool:

        return await self.exists(workspace_id, file_id, filename)

    async def get_path(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
    ) -> Path:
        return self._resolve_file_path(workspace_id, file_id, filename)

    async def delete_workspace(
        self,
        workspace_id: str,
    ) -> bool:
        ws_dir = self._resolve_workspace_dir(workspace_id)
        return await asyncio.to_thread(self._sync_delete_workspace, ws_dir)
