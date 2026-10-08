"""Cheap-first OCR with escalation.

Analogy: ask the free scanner app first. Only if its own confidence is low (or it
found almost nothing) do you pay the expert service to re-read the page.
"""
from __future__ import annotations

from claimlens.ocr.base import OcrEngine, OcrError, OcrResult


class CascadeOcr:
    def __init__(self, primary: OcrEngine, secondary: OcrEngine, min_confidence: float = 80.0, min_chars_per_page: int = 100):
        self.primary, self.secondary = primary, secondary
        self.min_confidence, self.min_chars_per_page = min_confidence, min_chars_per_page
        self.name = f"{primary.name}+{secondary.name}"

    def _good_enough(self, r: OcrResult) -> bool:
        dense = len(r.text.strip()) / max(r.pages, 1) >= self.min_chars_per_page
        confident = r.mean_confidence is None or r.mean_confidence >= self.min_confidence
        return dense and confident

    def extract(self, data: bytes, filename: str) -> OcrResult:
        first = self.primary.extract(data, filename)
        if self._good_enough(first):
            return first
        try:
            second = self.secondary.extract(data, filename)
        except OcrError:
            return first  # keep the cheap result; its low confidence will flag the doc for human review
        return OcrResult(
            text=second.text, pages=second.pages, engine=second.engine, mean_confidence=second.mean_confidence,
            cost_usd=round(first.cost_usd + second.cost_usd, 6),  # you paid for BOTH reads
            escalated=True, engines_used=[first.engine, second.engine],
        )
