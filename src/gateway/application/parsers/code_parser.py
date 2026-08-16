

from __future__ import annotations

from pathlib import Path
from src.gateway.application.parsers.base import BaseParser

CODE_EXTENSIONS = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".cpp",
    ".c",
    ".cc",
    ".cxx",
    ".h",
    ".hpp",
    ".hxx",
    ".java",
    ".rb",
    ".php",
    ".cs",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".html",
    ".htm",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".swift",
    ".kt",
    ".kts",
    ".scala",
    ".r",
    ".m",
    ".dart",
    ".lua",
    ".zig",
    ".nim",
    ".ex",
    ".exs",
    ".erl",
    ".hs",
    ".vue",
    ".svelte",
    ".proto",
    ".graphql",
    ".gql",
}

CODE_MIME_TYPES = {
    "text/x-python",
    "text/x-script.python",
    "application/x-python-code",
    "application/javascript",
    "text/javascript",
    "application/typescript",
    "text/x-go",
    "text/x-c",
    "text/x-c++",
    "text/x-rust",
    "text/x-java-source",
    "text/x-ruby",
    "text/x-php",
    "text/x-csharp",
    "text/x-shellscript",
    "application/x-sh",
    "text/x-sql",
    "text/html",
    "text/css",
}

class CodeParser(BaseParser):

    def can_parse(self, filename: str, mime_type: str = "") -> bool:
        ext = Path(filename).suffix.lower()
        if ext in CODE_EXTENSIONS:
            return True
        if mime_type:
            mime_lower = mime_type.lower()
            if mime_lower in CODE_MIME_TYPES or mime_lower.startswith("text/x-"):
                return True
        return False

    def parse(self, content: bytes, filename: str = "") -> str:

        if not content:
            return ""

        if content.startswith(b"\xef\xbb\xbf"):
            content = content[3:]

        return content.decode("utf-8", errors="replace")
