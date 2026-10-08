"""Common shape every parser returns.

Analogy: each parser is a different tool (letter opener, box cutter, fax reader),
but they all hand the next station the same "document summary card".
"""
from __future__ import annotations

from dataclasses import dataclass, field


class ParseError(Exception):
    """The file is broken, encrypted, or otherwise unreadable."""


class UnsupportedFileType(Exception):
    """We have no parser for this extension."""


@dataclass
class ParsedDocument:
    filename: str
    file_type: str
    text: str
    page_count: int = 1
    needs_ocr: bool = False
    warnings: list[str] = field(default_factory=list)
    attachments: list["ParsedDocument"] = field(default_factory=list)
    raw_bytes: bytes | None = None  # kept only when needs_ocr, so the OCR step can read the original

    @property
    def chars_per_page(self) -> float:
        return len(self.text.strip()) / max(self.page_count, 1)

    @property
    def any_needs_ocr(self) -> bool:
        return self.needs_ocr or any(a.any_needs_ocr for a in self.attachments)

    def full_text(self) -> str:
        """Body text plus the text of any attachments (clearly separated)."""
        parts = [self.text.strip()]
        for att in self.attachments:
            parts.append(f"\n--- ATTACHMENT: {att.filename} ---\n{att.full_text()}")
        return "\n".join(p for p in parts if p)
