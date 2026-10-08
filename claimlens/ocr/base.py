"""OCR interface shared by every engine.

Analogy: three different "readers" (a free scanner app, a paid expert service, a
tireless-but-literal oracle for demos) that all hand back the same thing:
the text, how sure they are, and what it cost.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from claimlens.parsers.base import ParsedDocument


class OcrError(RuntimeError):
    """OCR could not run or produced nothing. Never includes document text.
    `transient=True` means retrying later may succeed (throttling, timeouts)."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass
class OcrResult:
    text: str
    pages: int
    engine: str
    mean_confidence: Optional[float] = None  # 0-100; None if the engine can't tell
    cost_usd: float = 0.0
    escalated: bool = False
    engines_used: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.engines_used:
            self.engines_used = [self.engine]


class OcrEngine(Protocol):
    name: str

    def extract(self, data: bytes, filename: str) -> OcrResult: ...


@dataclass
class OcrSummary:
    pages: int = 0
    cost_usd: float = 0.0
    documents: int = 0
    engines: list[str] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    escalated: bool = False

    @property
    def mean_confidence(self) -> Optional[float]:
        return sum(self.confidences) / len(self.confidences) if self.confidences else None


def _walk(doc: ParsedDocument):
    yield doc
    for att in doc.attachments:
        yield from _walk(att)


def apply_ocr(parsed: ParsedDocument, engine: OcrEngine) -> OcrSummary:
    """Run OCR on every file in the tree (the file itself and any email attachments) that needs it,
    and replace its empty/sparse text with the OCR text."""
    summary = OcrSummary()
    for node in _walk(parsed):
        if not node.needs_ocr:
            continue
        if node.raw_bytes is None:
            raise OcrError(f"{node.filename}: original bytes unavailable for OCR")
        result = engine.extract(node.raw_bytes, node.filename)
        if not result.text.strip():
            raise OcrError(f"{node.filename}: OCR recognized no text")
        node.text = result.text
        node.page_count = max(result.pages, 1)
        node.needs_ocr = False
        node.raw_bytes = None
        node.warnings.append(f"Text recovered by OCR ({'+'.join(result.engines_used)})")
        summary.pages += result.pages
        summary.cost_usd = round(summary.cost_usd + result.cost_usd, 6)
        summary.documents += 1
        summary.escalated = summary.escalated or result.escalated
        for name in result.engines_used:
            if name not in summary.engines:
                summary.engines.append(name)
        if result.mean_confidence is not None:
            summary.confidences.append(result.mean_confidence)
    return summary
