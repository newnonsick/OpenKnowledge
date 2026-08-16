"""Unit tests for multi-format document parsers."""

from __future__ import annotations

import json
import pytest

from src.gateway.application.parsers import (
    CodeParser,
    JSONParser,
    PDFParser,
    TextParser,
    get_parser_for_file,
)
from src.gateway.domain.exceptions import ValidationException


# ==============================================================================
# TextParser Tests
# ==============================================================================

def test_text_parser_plain_text():
    """Verify parsing plain text content."""
    parser = TextParser()
    content = b"Hello world! This is plain text."
    result = parser.parse(content, "test.txt")
    assert result == "Hello world! This is plain text."


def test_text_parser_with_bom():
    """Verify UTF-8 BOM is stripped correctly."""
    parser = TextParser()
    content = b"\xef\xbb\xbfMarkdown header\n\nContent paragraph."
    result = parser.parse(content, "doc.md")
    assert result == "Markdown header\n\nContent paragraph."
    assert not result.startswith("\ufeff")


def test_text_parser_corrupted_utf8():
    """Verify corrupted byte sequences decode with replacement character."""
    parser = TextParser()
    corrupted = b"Good prefix \xff\xfe bad suffix"
    result = parser.parse(corrupted, "notes.txt")
    assert "Good prefix" in result
    assert "bad suffix" in result
    assert "\ufffd" in result


def test_text_parser_empty_content():
    """Verify empty byte content produces empty string."""
    parser = TextParser()
    assert parser.parse(b"", "empty.txt") == ""


# ==============================================================================
# CodeParser Tests
# ==============================================================================

def test_code_parser_python():
    """Verify Python code extraction preserves indentation."""
    parser = CodeParser()
    code = (
        b"def add(a: int, b: int) -> int:\n"
        b"    \"\"\"Add two integers.\"\"\"\n"
        b"    return a + b\n"
    )
    result = parser.parse(code, "math_utils.py")
    assert "def add(a: int, b: int) -> int:" in result
    assert "    return a + b" in result


def test_code_parser_javascript():
    """Verify JavaScript / TypeScript code parsing."""
    parser = CodeParser()
    js_code = b"export const fetchData = async (url) => {\n  const res = await fetch(url);\n  return res.json();\n};\n"
    result = parser.parse(js_code, "api.ts")
    assert "export const fetchData" in result


# ==============================================================================
# PDFParser Tests
# ==============================================================================

def test_pdf_parser_corrupted_header_raises_validation():
    """Verify non-PDF bytes raise ValidationException."""
    parser = PDFParser()
    with pytest.raises(ValidationException) as exc_info:
        parser.parse(b"NOT_A_PDF_STREAM", "sample.pdf")
    assert "invalid pdf header" in exc_info.value.message.lower()


def test_pdf_parser_empty_content():
    """Verify 0-byte content produces empty string."""
    parser = PDFParser()
    assert parser.parse(b"", "empty.pdf") == ""


def test_pdf_parser_can_parse():
    """Verify PDFParser recognizes .pdf extension and application/pdf mime type."""
    parser = PDFParser()
    assert parser.can_parse("document.pdf") is True
    assert parser.can_parse("doc.PDF") is True
    assert parser.can_parse("file.bin", "application/pdf") is True
    assert parser.can_parse("script.py") is False


# ==============================================================================
# JSONParser Tests
# ==============================================================================

def test_json_parser_valid_object():
    """Verify parsing valid JSON object."""
    parser = JSONParser()
    data = {"name": "AI Gateway", "version": "1.0.0", "features": ["rag", "occ", "streaming"]}
    raw_bytes = json.dumps(data).encode("utf-8")
    result = parser.parse(raw_bytes, "config.json")
    parsed_back = json.loads(result)
    assert parsed_back["name"] == "AI Gateway"
    assert len(parsed_back["features"]) == 3


def test_json_parser_valid_list():
    """Verify parsing JSON array."""
    parser = JSONParser()
    data = [{"id": 1, "title": "First"}, {"id": 2, "title": "Second"}]
    raw_bytes = json.dumps(data).encode("utf-8")
    result = parser.parse(raw_bytes, "items.json")
    assert "First" in result
    assert "Second" in result


def test_json_parser_malformed_raises_validation():
    """Verify malformed JSON raises ValidationException."""
    parser = JSONParser()
    malformed = b'{"name": "broken", "list": [1, 2,'
    with pytest.raises(ValidationException) as exc_info:
        parser.parse(malformed, "broken.json")
    assert "malformed json" in exc_info.value.message.lower()


def test_json_parser_empty_content():
    """Verify empty content produces empty string."""
    parser = JSONParser()
    assert parser.parse(b"", "empty.json") == ""


# ==============================================================================
# Parser Registry / get_parser_for_file Tests
# ==============================================================================

def test_get_parser_for_file_selection():
    """Verify get_parser_for_file selects appropriate parser instance."""
    assert isinstance(get_parser_for_file("doc.pdf"), PDFParser)
    assert isinstance(get_parser_for_file("data.json"), JSONParser)
    assert isinstance(get_parser_for_file("app.py"), CodeParser)
    assert isinstance(get_parser_for_file("server.ts"), CodeParser)
    assert isinstance(get_parser_for_file("main.rs"), CodeParser)
    assert isinstance(get_parser_for_file("README.md"), TextParser)
    assert isinstance(get_parser_for_file("notes.txt"), TextParser)
    assert isinstance(get_parser_for_file("unknown_format.xyz"), TextParser)  # Fallback
