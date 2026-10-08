"""Demo OCR 'oracle': looks up the true text the data generator saved for each scan.

It exists ONLY so the OCR *plumbing* (routing, cost, cascade, eval) can be exercised
without AWS or even Tesseract. It reads no pixels. Optional error_rate randomly
garbles characters to simulate a sloppy engine. Never quote its accuracy.
"""
from __future__ import annotations

import hashlib
import random
from pathlib import Path

from claimlens.llm.pricing import estimate_ocr_cost
from claimlens.ocr.base import OcrError, OcrResult

_CONFUSIONS = {"0": "O", "1": "l", "5": "S", "8": "B", "2": "Z"}


class OracleOcr:
    name = "oracle"

    def __init__(self, oracle_dir: Path, error_rate: float = 0.0, seed: int = 0):
        self.dir, self.error_rate, self.seed = Path(oracle_dir), error_rate, seed

    def extract(self, data: bytes, filename: str) -> OcrResult:
        digest = hashlib.sha1(data).hexdigest()
        path = self.dir / f"{digest}.txt"
        if not path.exists():
            raise OcrError(f"{filename}: no oracle text for this file (is oracle_dir correct?)")
        raw = path.read_text()
        pages = max(1, raw.count("\f") + 1)
        text = raw.replace("\f", "\n\n")
        if self.error_rate > 0:
            rng = random.Random(f"{self.seed}:{digest}")
            text = "".join(_CONFUSIONS.get(ch, ch) if ch in _CONFUSIONS and rng.random() < self.error_rate else ch for ch in text)
        conf = max(40.0, 98.0 - 1000 * self.error_rate)
        return OcrResult(text=text, pages=pages, engine=self.name, mean_confidence=conf,
                         cost_usd=estimate_ocr_cost(self.name, pages))
