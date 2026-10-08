from __future__ import annotations

from claimlens.parsers.base import ParsedDocument


def parse_text(data: bytes, filename: str) -> ParsedDocument:
    # errors="replace" so one odd byte can't crash the batch.
    return ParsedDocument(filename=filename, file_type="txt", text=data.decode("utf-8", errors="replace"))
