

from __future__ import annotations

import io
from pathlib import Path
from src.gateway.application.parsers.base import BaseParser
from src.gateway.domain.exceptions import ValidationException

class PDFParser(BaseParser):

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

            pages_text = []
            for page in reader.pages:
                text = page.extract_text()
                if text and text.strip():
                    pages_text.append(text.strip())

            return "\n\n".join(pages_text)
        except Exception as exc:
            raise ValidationException(f"Corrupted or unreadable PDF document: {exc}") from exc
