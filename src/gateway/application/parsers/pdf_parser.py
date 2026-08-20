

from __future__ import annotations

import io
from pathlib import Path
from src.gateway.application.parsers.base import BaseParser
from src.gateway.domain.exceptions import ValidationException

class PDFParser(BaseParser):

    def __init__(
        self,
        *,
        max_pages: int | None = None,
        max_output_characters: int | None = None,
    ) -> None:
        if max_pages is not None and max_pages <= 0:
            raise ValueError("PDF page limit must be positive")
        if max_output_characters is not None and max_output_characters <= 0:
            raise ValueError("PDF output limit must be positive")
        self._max_pages = max_pages
        self._max_output_characters = max_output_characters

    def can_parse(self, filename: str, mime_type: str = "") -> bool:
        ext = Path(filename).suffix.lower()
        if ext == ".pdf":
            return True
        if mime_type:
            mime_lower = mime_type.lower()
            if mime_lower == "application/pdf" or "pdf" in mime_lower:
                return True
        return False

    def parse(self, content: bytes, filename: str = "") -> str:

        if not content:
            return ""

        if not content.startswith(b"%PDF-"):
            raise ValidationException("Corrupted or invalid PDF header.")

        try:
            import pypdf
        except ImportError:
            raise ValidationException("pypdf library is required to extract text from PDF files.")

        try:
            stream = io.BytesIO(content)
            reader = pypdf.PdfReader(stream)
            if self._max_pages is not None and len(reader.pages) > self._max_pages:
                raise ValidationException("PDF page limit exceeded.")

            pages_text = []
            total_characters = 0
            for page in reader.pages:
                text = page.extract_text()
                if text and text.strip():
                    normalized = text.strip()
                    total_characters += len(normalized)
                    if (
                        self._max_output_characters is not None
                        and total_characters > self._max_output_characters
                    ):
                        raise ValidationException("PDF extracted text limit exceeded.")
                    pages_text.append(normalized)

            return "\n\n".join(pages_text)
        except Exception as exc:
            raise ValidationException("Corrupted or unreadable PDF document.") from exc
