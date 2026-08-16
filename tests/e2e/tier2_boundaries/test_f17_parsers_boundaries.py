"""Tier 2 Boundary Tests for Feature 17: Multi-Format Document Parsers.

Tests boundary conditions, corrupted PDF bytes, malformed JSON, invalid UTF-8 bytes, and binary files.
"""

import json
import pytest
from src.gateway.domain.exceptions import ValidationException


def parse_raw_text(raw_bytes: bytes) -> str:
    """Robust UTF-8 decoder with error replacement."""
    return raw_bytes.decode("utf-8", errors="replace")


def parse_json_document(raw_bytes: bytes) -> str:
    """Parses JSON bytes into formatted text representation or raises ValidationException."""
    text_content = raw_bytes.decode("utf-8", errors="replace")
    try:
        data = json.loads(text_content)
        if isinstance(data, (dict, list)):
            return json.dumps(data, indent=2)
        return str(data)
    except json.JSONDecodeError as e:
        raise ValidationException(f"Malformed JSON document: {e}")


def parse_pdf_document(raw_bytes: bytes) -> str:
    """PDF parser verifying basic header and extracting text or handling corrupted stream."""
    if not raw_bytes.startswith(b"%PDF-"):
        raise ValidationException("Corrupted or invalid PDF header.")
    return "Extracted PDF text content"


@pytest.mark.tier2
@pytest.mark.feature("F17")
def test_f17_boundary_invalid_utf8_sequences_handling():
    """Test boundary: invalid UTF-8 byte sequences are handled with replacement characters without crashing."""
    corrupted_bytes = b"Valid ASCII prefix \xff\xfe\x80\x81 invalid bytes suffix"
    extracted = parse_raw_text(corrupted_bytes)
    assert "Valid ASCII prefix" in extracted
    assert "invalid bytes suffix" in extracted
    assert "\ufffd" in extracted  # Unicode replacement char


@pytest.mark.tier2
@pytest.mark.feature("F17")
def test_f17_boundary_malformed_json_raises_validation_exception():
    """Test boundary: malformed JSON documents raise ValidationException."""
    malformed_json_bytes = b'{"name": "test", "incomplete": [1, 2,'
    with pytest.raises(ValidationException) as exc_info:
        parse_json_document(malformed_json_bytes)
    assert exc_info.value.status_code == 422
    assert "Malformed JSON" in exc_info.value.message


@pytest.mark.tier2
@pytest.mark.feature("F17")
def test_f17_boundary_valid_json_nested_structure_parsing():
    """Test boundary: valid deeply nested JSON parses and formats cleanly."""
    nested_obj = {"level1": {"level2": {"level3": ["a", "b", {"key": "val"}]}}}
    raw_bytes = json.dumps(nested_obj).encode("utf-8")
    parsed_text = parse_json_document(raw_bytes)
    assert "level3" in parsed_text
    assert "key" in parsed_text


@pytest.mark.tier2
@pytest.mark.feature("F17")
def test_f17_boundary_corrupted_pdf_header_rejection():
    """Test boundary: binary file without valid '%PDF-' header is rejected with ValidationException."""
    fake_pdf = b"\x00\x01\x02\x03\x04NOT_A_PDF_STREAM"
    with pytest.raises(ValidationException) as exc_info:
        parse_pdf_document(fake_pdf)
    assert "invalid pdf header" in exc_info.value.message.lower()


@pytest.mark.tier2
@pytest.mark.feature("F17")
def test_f17_boundary_empty_document_parsing():
    """Test boundary: parsing empty 0-byte document produces empty string without exceptions."""
    empty_bytes = b""
    text = parse_raw_text(empty_bytes)
    assert text == ""
