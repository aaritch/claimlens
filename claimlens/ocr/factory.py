"""Build an OCR engine from a short spec string.

  none                 -> no OCR (scanned docs stop with status needs_ocr)
  tesseract            -> local Tesseract
  textract             -> AWS Textract
  tesseract+textract   -> Tesseract first, escalate to Textract if unsure (cost saver)
  oracle[:0.02]        -> demo oracle, optional error rate
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from claimlens.config import Settings
from claimlens.ocr.base import OcrEngine, OcrError
from claimlens.ocr.cascade import CascadeOcr
from claimlens.ocr.oracle import OracleOcr
from claimlens.ocr.tesseract import TesseractEngine
from claimlens.ocr.textract import TextractEngine


def _single(spec: str, settings: Settings, oracle_dir: Optional[Path]) -> OcrEngine:
    if spec == "tesseract":
        return TesseractEngine()
    if spec == "textract":
        return TextractEngine(region=settings.region, bucket=settings.textract_bucket,
                              poll_seconds=settings.ocr_poll_seconds, timeout_seconds=settings.ocr_timeout_seconds)
    if spec.startswith("oracle"):
        if oracle_dir is None:
            raise OcrError("oracle OCR needs the data folder's ocr_oracle/ directory")
        rate = float(spec.split(":", 1)[1]) if ":" in spec else 0.0
        return OracleOcr(oracle_dir, error_rate=rate)
    raise OcrError(f"Unknown OCR engine '{spec}' (use none, tesseract, textract, tesseract+textract, oracle)")


def build_ocr(spec: str, settings: Settings, oracle_dir: Optional[Path] = None) -> Optional[OcrEngine]:
    spec = (spec or "none").strip().lower()
    if spec == "none":
        return None
    if "+" in spec:
        first, second = spec.split("+", 1)
        return CascadeOcr(_single(first, settings, oracle_dir), _single(second, settings, oracle_dir),
                          min_confidence=settings.min_ocr_confidence, min_chars_per_page=settings.min_chars_per_page)
    return _single(spec, settings, oracle_dir)
