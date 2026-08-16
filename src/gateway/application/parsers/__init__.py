

from __future__ import annotations

from typing import List
from src.gateway.application.parsers.base import BaseParser
from src.gateway.application.parsers.code_parser import CodeParser
from src.gateway.application.parsers.json_parser import JSONParser
from src.gateway.application.parsers.pdf_parser import PDFParser
from src.gateway.application.parsers.text_parser import TextParser
from src.gateway.application.parsers.chunker import Chunker, fixed_chunk_text

PARSERS: List[BaseParser] = [
    PDFParser(),
    JSONParser(),
    CodeParser(),
    TextParser(),
]

def get_parser_for_file(filename: str, mime_type: str = "") -> BaseParser:

    for parser in PARSERS:
        if parser.can_parse(filename, mime_type):
            return parser
    return TextParser()

__all__ = [
    "BaseParser",
    "TextParser",
    "CodeParser",
    "PDFParser",
    "JSONParser",
    "Chunker",
    "fixed_chunk_text",
    "get_parser_for_file",
    "PARSERS",
]
