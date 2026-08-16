

from __future__ import annotations

from abc import ABC, abstractmethod

class BaseParser(ABC):

    @abstractmethod
    def parse(self, content: bytes, filename: str = "") -> str:

        ...

    @abstractmethod
    def can_parse(self, filename: str, mime_type: str = "") -> bool:

        ...
