

from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, Union
from uuid import UUID

class IFileStorage(ABC):

    @abstractmethod
    async def save_file(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
        content: Union[bytes, BinaryIO],
    ) -> Path:

        ...

    @abstractmethod
    async def read_file(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
    ) -> bytes:

        ...

    @abstractmethod
    async def delete_file(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
    ) -> bool:

        ...

    @abstractmethod
    async def exists(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
    ) -> bool:

        ...

    @abstractmethod
    async def get_path(
        self,
        workspace_id: str,
        file_id: Union[str, UUID],
        filename: str,
    ) -> Path:

        ...

    @abstractmethod
    async def delete_workspace(
        self,
        workspace_id: str,
    ) -> bool:

        ...
