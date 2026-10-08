"""Scoring math: pure functions, no I/O, easy to test.

Analogy: grading a school exam. For each question (field) we compare the
student's answer (model output) with the answer key (ground truth) and sort it
into one of five buckets:

  correct       key has a value, student wrote the same value        -> TP
  both_null     key is blank, student left it blank                  -> TN
  missed        key has a value, student left it blank               -> FN
  hallucinated  key is blank, student invented a value               -> FP
  wrong_value   key has a value, student wrote a DIFFERENT value     -> FP *and* FN

Precision = "when the model answers, how often is it right?"  TP / (TP + FP)
Recall    = "of the answers that exist, how many did it find?" TP / (TP + FN)
F1        = one blended score (harmonic mean): 2TP / (2TP + FP + FN)

Why both? A model that answers only when 100% sure has great precision but poor
recall. A model that always guesses has great recall but poor precision. For
regulated data, a confident WRONG answer is usually worse than a blank one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

FIELDS = [
    "provider_npi", "provider_name", "payer_name", "claim_reference",
    "service_date", "disputed_amount", "billing_violation_type",
]  # 'summary' is free text, so it's excluded from exact-match scoring

CORRECT, BOTH_NULL, MISSED, HALLUCINATED, WRONG = "correct", "both_null", "missed", "hallucinated", "wrong_value"
GOOD_OUTCOMES = {CORRECT, BOTH_NULL}


def normalize(field: str, value: Any) -> Any:
    """Make equivalent answers compare equal ('Aetna ' == 'aetna', 1450.5 == 1450.50)."""
    if value is None or value == "":
        return None
    if field == "disputed_amount":
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return str(value)
    if field == "provider_npi":
        return "".join(ch for ch in str(value) if ch.isdigit())
    return " ".join(str(value).split()).casefold()


def classify_outcome(field: str, expected: Any, predicted: Any) -> str:
    e, p = normalize(field, expected), normalize(field, predicted)
    if e is None and p is None:
        return BOTH_NULL
    if e is None:
        return HALLUCINATED
    if p is None:
        return MISSED
    return CORRECT if e == p else WRONG


def score_document(expected: dict, predicted: Optional[dict]) -> dict[str, str]:
    """Outcome per field. predicted=None means the pipeline produced no claim at all."""
    predicted = predicted or {}
    return {f: classify_outcome(f, expected.get(f), predicted.get(f)) for f in FIELDS}


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, outcome: str) -> None:
        if outcome == CORRECT:
            self.tp += 1
        elif outcome == BOTH_NULL:
            self.tn += 1
        elif outcome == MISSED:
            self.fn += 1
        elif outcome == HALLUCINATED:
            self.fp += 1
        elif outcome == WRONG:
            self.fp += 1
            self.fn += 1

    def merge(self, other: "Counts") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        self.tn += other.tn

    @property
    def precision(self) -> Optional[float]:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> Optional[float]:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None

    @property
    def f1(self) -> Optional[float]:
        denom = 2 * self.tp + self.fp + self.fn
        return (2 * self.tp / denom) if denom else None


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (simple and explainable)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]
