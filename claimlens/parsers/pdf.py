from __future__ import annotations

import io

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from claimlens.parsers.base import ParsedDocument, ParseError


def parse_pdf(data: bytes, filename: str, min_chars_per_page: int = 100) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ParseError(f"{filename}: PDF is encrypted")
        pages = [(page.extract_text() or "") for page in reader.pages]
    except PdfReadError as exc:
        raise ParseError(f"{filename}: unreadable PDF ({exc.__class__.__name__})") from exc

    doc = ParsedDocument(filename=filename, file_type="pdf", text="\n\n".join(pages), page_count=max(len(pages), 1))
    # Density check (per page, not total!): a 20-page scan with a stray header
    # line would look "fine" on total characters but be mostly empty.
    if doc.chars_per_page < min_chars_per_page:
        doc.needs_ocr = True
        doc.raw_bytes = data
        doc.warnings.append(f"Low text density ({doc.chars_per_page:.0f} chars/page): probably scanned, OCR needed")
    return doc
