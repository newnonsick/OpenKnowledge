

from __future__ import annotations

from pathlib import Path
from src.gateway.application.parsers.base import BaseParser

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".csv",
    ".tsv",
    ".log",
    ".yaml",
    ".yml",
    ".ini",
    ".env",
    ".toml",
    ".conf",
    ".cfg",
}

class TextParser(BaseParser):

    def can_parse(self, filename: str, mime_type: str = "") -> bool:
        ext = Path(filename).suffix.lower()
        if ext in TEXT_EXTENSIONS:
            return True
        if mime_type:
            mime_lower = mime_type.lower()
            if mime_lower.startswith("text/") or "markdown" in mime_lower:
                return True
        return False

    def parse(self, content: bytes, filename: str = "") -> str:

        if not content:
            return ""

        if content.startswith(b"\xef\xbb\xbf"):
            content = content[3:]

        return content.decode("utf-8", errors="replace")
