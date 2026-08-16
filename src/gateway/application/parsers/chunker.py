

from __future__ import annotations

import re
from typing import List, Optional

def fixed_chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:

    if not text:
        return []

    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0")

    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be >= 0 and strictly less than chunk_size")

    chunks: List[str] = []
    start = 0
    text_len = len(text)
    step = chunk_size - overlap

    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunk = text[start:end]
        chunks.append(chunk)
        if end == text_len:
            break
        start += step

    return chunks

class Chunker:

    def __init__(self, default_chunk_size: int = 500, default_overlap: int = 50) -> None:
        self.default_chunk_size = default_chunk_size
        self.default_overlap = default_overlap

    def chunk_fixed(
        self,
        text: str,
        chunk_size: Optional[int] = None,
        overlap: Optional[int] = None,
    ) -> List[str]:

        size = chunk_size if chunk_size is not None else self.default_chunk_size
        ol = overlap if overlap is not None else self.default_overlap
        return fixed_chunk_text(text, chunk_size=size, overlap=ol)

    def chunk_semantic(
        self,
        text: str,
        chunk_size: Optional[int] = None,
        overlap: Optional[int] = None,
    ) -> List[str]:

        if not text:
            return []

        size = chunk_size if chunk_size is not None else self.default_chunk_size
        ol = overlap if overlap is not None else self.default_overlap

        if size <= 0:
            raise ValueError("chunk_size must be greater than 0")
        if ol < 0 or ol >= size:
            raise ValueError("overlap must be >= 0 and strictly less than chunk_size")

        if len(text) <= size:
            return [text]

        split_pattern = r"(?=\n#{1,6}\s)|(?<=\n\n)|(?<=\n```\n)"
        raw_sections = [s for s in re.split(split_pattern, text) if s]

        if not raw_sections:
            return fixed_chunk_text(text, chunk_size=size, overlap=ol)

        chunks: List[str] = []
        current_chunk = ""

        for section in raw_sections:
            if len(section) > size:

                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""

                sub_chunks = fixed_chunk_text(section, chunk_size=size, overlap=ol)
                chunks.extend(sub_chunks)
            elif len(current_chunk) + len(section) <= size:
                current_chunk += section
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())

                if ol > 0 and current_chunk:
                    tail = current_chunk[-ol:]
                    current_chunk = tail + section
                else:
                    current_chunk = section

        if current_chunk and current_chunk.strip():
            chunks.append(current_chunk.strip())

        return chunks if chunks else [text]
