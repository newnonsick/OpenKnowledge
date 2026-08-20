import time

import pytest

from src.gateway.application.services.bounded_document_parser import BoundedDocumentParser
from src.gateway.domain.exceptions import ParserTimeoutException, ValidationException


def _slow_worker(
    connection,
    filename,
    mime_type,
    content,
    memory_limit_bytes,
    cpu_seconds,
    max_pages,
    max_output_characters,
):
    time.sleep(2)


async def test_parser_validates_media_contract_and_extracts_text() -> None:
    parser = BoundedDocumentParser(timeout_seconds=5, memory_limit_bytes=128 * 1024 * 1024)
    result = await parser.parse(
        filename="notes.txt",
        mime_type="text/plain",
        content="สวัสดี family".encode(),
    )
    assert result.text == "สวัสดี family"
    assert result.parser_version.startswith("text-")

    with pytest.raises(ValidationException):
        await parser.parse(
            filename="invoice.pdf",
            mime_type="text/plain",
            content=b"not a pdf",
        )
    with pytest.raises(ValidationException):
        await parser.parse(
            filename="invoice.txt",
            mime_type="application/pdf",
            content=b"%PDF-1.7",
        )


async def test_parser_process_is_terminated_at_timeout() -> None:
    parser = BoundedDocumentParser(
        timeout_seconds=0.05,
        memory_limit_bytes=128 * 1024 * 1024,
        worker_target=_slow_worker,
    )
    with pytest.raises(ParserTimeoutException):
        await parser.parse(
            filename="notes.txt",
            mime_type="text/plain",
            content=b"payload",
        )


async def test_parser_rejects_extracted_text_over_limit() -> None:
    parser = BoundedDocumentParser(
        timeout_seconds=5,
        memory_limit_bytes=128 * 1024 * 1024,
        max_output_characters=4,
    )
    with pytest.raises(ValidationException):
        await parser.parse(
            filename="notes.txt",
            mime_type="text/plain",
            content=b"12345",
        )
