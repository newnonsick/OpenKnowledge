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


def _send_then_linger_worker(
    connection,
    filename,
    mime_type,
    content,
    memory_limit_bytes,
    cpu_seconds,
    max_pages,
    max_output_characters,
):
    connection.send(("ok", "late-exit", "text-v1"))
    connection.close()
    time.sleep(5)


def _crash_worker(
    connection,
    filename,
    mime_type,
    content,
    memory_limit_bytes,
    cpu_seconds,
    max_pages,
    max_output_characters,
):
    import os

    os._exit(1)


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


async def test_parser_delivers_result_larger_than_pipe_buffer() -> None:
    parser = BoundedDocumentParser(
        timeout_seconds=30,
        memory_limit_bytes=256 * 1024 * 1024,
    )
    content = ("parse-result line สวัสดี family\n" * 22000).encode()
    assert len(content) > 512 * 1024
    result = await parser.parse(
        filename="large-notes.txt",
        mime_type="text/plain",
        content=content,
    )
    assert result.text == content.decode()
    assert result.parser_version.startswith("text-")


async def test_parser_returns_valid_result_despite_slow_child_exit() -> None:
    parser = BoundedDocumentParser(
        timeout_seconds=30,
        memory_limit_bytes=256 * 1024 * 1024,
        worker_target=_send_then_linger_worker,
    )
    result = await parser.parse(
        filename="notes.txt",
        mime_type="text/plain",
        content="สวัสดี family".encode(),
    )
    assert result.text == "late-exit"


async def test_parser_child_crash_maps_to_validation_error() -> None:
    parser = BoundedDocumentParser(
        timeout_seconds=30,
        memory_limit_bytes=256 * 1024 * 1024,
        worker_target=_crash_worker,
    )
    with pytest.raises(ValidationException):
        await parser.parse(
            filename="notes.txt",
            mime_type="text/plain",
            content=b"payload",
        )
