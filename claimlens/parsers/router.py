"""Pick the right parser by file extension.

Analogy: the mail-room sorter who looks at the outside of each item and drops it
in the right bin.
"""
from __future__ import annotations

from pathlib import Path

from claimlens.parsers.base import ParsedDocument, UnsupportedFileType
from claimlens.parsers.eml import parse_eml
from claimlens.parsers.pdf import parse_pdf
from claimlens.parsers.plain import parse_text
from claimlens.parsers.word import parse_docx

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def parse_bytes(data: bytes, filename: str, min_chars_per_page: int = 100, _depth: int = 0) -> ParsedDocument:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return parse_pdf(data, filename, min_chars_per_page)
    if ext == ".docx":
        return parse_docx(data, filename)
    if ext == ".eml":
        return parse_eml(data, filename, min_chars_per_page, _depth=_depth)
    if ext in (".txt", ".text"):
        return parse_text(data, filename)
    if ext in IMAGE_EXTENSIONS:
        # No text layer at all: this is an OCR job (Phase 3).
        return ParsedDocument(filename=filename, file_type="image", text="", needs_ocr=True,
                              warnings=["Image file: OCR needed"], raw_bytes=data)
    raise UnsupportedFileType(f"{filename}: unsupported file type '{ext}'")


def parse_file(path: Path, min_chars_per_page: int = 100) -> ParsedDocument:
    return parse_bytes(Path(path).read_bytes(), Path(path).name, min_chars_per_page)
