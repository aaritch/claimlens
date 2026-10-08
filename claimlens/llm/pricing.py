"""Cost math lives in config, not in code, so prices can change without a code change."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _load() -> dict:
    return json.loads((Path(__file__).parent / "pricing.json").read_text())


def estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    table = _load()
    rates = table["default"]
    for key, value in table.get("models", {}).items():
        if key in model_id:
            rates = value
            break
    cost = (input_tokens / 1000) * rates["input_per_1k"] + (output_tokens / 1000) * rates["output_per_1k"]
    return round(cost, 6)


def estimate_ocr_cost(engine: str, pages: int) -> float:
    """Per-page OCR cost. Tesseract is $0 in API fees (you still pay for the compute that runs it)."""
    rates = _load().get("ocr", {})
    return round(rates.get(engine, {"per_page": 0.0})["per_page"] * pages, 6)
