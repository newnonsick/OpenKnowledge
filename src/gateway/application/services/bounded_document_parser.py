from __future__ import annotations

import asyncio
from dataclasses import dataclass
import multiprocessing
from pathlib import Path

from src.gateway.application.parsers import get_parser_for_file
from src.gateway.application.parsers.code_parser import CODE_EXTENSIONS
from src.gateway.application.parsers.pdf_parser import PDFParser
from src.gateway.application.parsers.text_parser import TEXT_EXTENSIONS
from src.gateway.domain.exceptions import ParserTimeoutException, ValidationException


_JSON_EXTENSIONS = {".json", ".jsonl", ".ndjson", ".geojson"}
_JSON_MIME_TYPES = {"application/json", "application/x-ndjson", "text/json"}
_SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | CODE_EXTENSIONS | _JSON_EXTENSIONS | {".pdf"}


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    text: str
    parser_version: str


def _parser_version(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "pdf-pypdf-v1"
    if suffix in _JSON_EXTENSIONS:
        return "json-v1"
    if suffix in CODE_EXTENSIONS:
        return "code-v1"
    return "text-v1"


def _parse_worker(
    connection,
    filename: str,
    mime_type: str,
    content: bytes,
    memory_limit_bytes: int,
    cpu_seconds: int,
    max_pages: int,
    max_output_characters: int,
) -> None:
    try:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        except (ImportError, AttributeError, OSError, ValueError):
            pass
        parser = (
            PDFParser(
                max_pages=max_pages,
                max_output_characters=max_output_characters,
            )
            if Path(filename).suffix.lower() == ".pdf"
            else get_parser_for_file(filename, mime_type)
        )
        text = parser.parse(content, filename=filename)
        if len(text) > max_output_characters:
            raise ValidationException("Document extracted text limit exceeded.")
        connection.send(("ok", text, _parser_version(filename)))
    except Exception:
        connection.send(("error", None, None))
    finally:
        connection.close()


class BoundedDocumentParser:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        memory_limit_bytes: int,
        cpu_seconds: int = 30,
        max_pages: int = 500,
        max_output_characters: int = 5_000_000,
        worker_target=_parse_worker,
    ) -> None:
        if (
            timeout_seconds <= 0
            or memory_limit_bytes <= 0
            or cpu_seconds <= 0
            or max_pages <= 0
            or max_output_characters <= 0
        ):
            raise ValueError("Parser limits must be positive")
        self._timeout_seconds = timeout_seconds
        self._memory_limit_bytes = memory_limit_bytes
        self._cpu_seconds = cpu_seconds
        self._max_pages = max_pages
        self._max_output_characters = max_output_characters
        self._worker_target = worker_target

    async def parse(self, *, filename: str, mime_type: str, content: bytes) -> ParsedDocument:
        normalized_mime = self._validate_media(filename, mime_type, content)
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=self._worker_target,
            args=(
                child,
                filename,
                normalized_mime,
                content,
                self._memory_limit_bytes,
                self._cpu_seconds,
                self._max_pages,
                self._max_output_characters,
            ),
            daemon=True,
        )
        process.start()
        child.close()
        try:
            await asyncio.to_thread(process.join, self._timeout_seconds)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 2)
                raise ParserTimeoutException()
            if process.exitcode != 0 or not parent.poll():
                raise ValidationException("Document parsing failed.")
            state, text, parser_version = parent.recv()
            if state != "ok" or text is None or parser_version is None:
                raise ValidationException("Document parsing failed.")
            return ParsedDocument(text=text, parser_version=parser_version)
        finally:
            parent.close()
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 2)
            process.close()

    def _validate_media(self, filename: str, mime_type: str, content: bytes) -> str:
        suffix = Path(filename).suffix.lower()
        normalized_mime = mime_type.split(";", 1)[0].strip().lower()
        if suffix not in _SUPPORTED_EXTENSIONS:
            raise ValidationException("Unsupported document extension.")
        if suffix == ".pdf" or normalized_mime == "application/pdf" or content.startswith(b"%PDF-"):
            if suffix != ".pdf" or normalized_mime != "application/pdf" or not content.startswith(b"%PDF-"):
                raise ValidationException("PDF extension, MIME type, and file signature do not match.")
            return normalized_mime
        if suffix in _JSON_EXTENSIONS or normalized_mime in _JSON_MIME_TYPES:
            if suffix not in _JSON_EXTENSIONS or normalized_mime not in _JSON_MIME_TYPES:
                raise ValidationException("JSON extension and MIME type do not match.")
            return normalized_mime
        if not normalized_mime.startswith("text/") and normalized_mime not in {
            "application/javascript",
            "application/typescript",
            "application/x-python-code",
            "application/x-sh",
        }:
            raise ValidationException("Document MIME type is not supported for this extension.")
        if b"\x00" in content:
            raise ValidationException("Text documents cannot contain null bytes.")
        return normalized_mime
