

from __future__ import annotations

import json
from pathlib import Path
from src.gateway.application.parsers.base import BaseParser
from src.gateway.domain.exceptions import ValidationException

class JSONParser(BaseParser):

    def can_parse(self, filename: str, mime_type: str = "") -> bool:
        ext = Path(filename).suffix.lower()
        if ext in {".json", ".jsonl", ".ndjson", ".geojson"}:
            return True
        if mime_type:
            mime_lower = mime_type.lower()
            if mime_lower in {"application/json", "application/x-ndjson", "text/json"}:
                return True
        return False

    def parse(self, content: bytes, filename: str = "") -> str:

        if not content:
            return ""

        if content.startswith(b"\xef\xbb\xbf"):
            content = content[3:]

        text_content = content.decode("utf-8", errors="replace")

        try:
            data = json.loads(text_content)
            if isinstance(data, (dict, list)):
                return json.dumps(data, indent=2, ensure_ascii=False)
            return str(data)
        except json.JSONDecodeError as exc:

            lines = [line.strip() for line in text_content.splitlines() if line.strip()]
            if lines:
                parsed_records = []
                try:
                    for line in lines:
                        parsed_records.append(json.loads(line))
                    return json.dumps(parsed_records, indent=2, ensure_ascii=False)
                except Exception:
                    pass
            raise ValidationException("Malformed JSON document.") from exc
