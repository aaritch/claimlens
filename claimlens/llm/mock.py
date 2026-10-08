"""Offline stand-in for Bedrock so you can run the whole pipeline with no AWS account.

Analogy: a crash-test dummy. It lets you test the seatbelts (parsers, rules,
pipeline) but tells you NOTHING about how a real driver (the real model) would
behave. Never quote its accuracy. It only understands the synthetic documents'
labelled-line style; a real model handles messy prose.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from claimlens.llm.bedrock_client import ToolCallResult
from claimlens.rules.payers import PAYER_ALIASES

_NPI = re.compile(r"(?:NPI|National\s+Provider\s+Identifier)\D{0,40}(\d{10})", re.I)
_AMOUNT = re.compile(r"(?:\$|USD\s*)\s*([\d,]+\.\d{2})")
_CLAIM = re.compile(r"\b(CLM-\d{6})\b")
_DATE_LINE = re.compile(r"^(?:Date of service|Service date|DOS)\s*:\s*(.+?)\s*$", re.I | re.M)
_PROVIDER = re.compile(r"^(?:Billing provider|Facility/Provider|Provider name)\s*:\s*(.+?)\s*(?:\(|,|$)", re.I | re.M)
_PAYER = re.compile(r"^(?:Health plan|Insurer|Payer)\s*:\s*(.+?)\s*$", re.I | re.M)


def _iso_date(raw: str) -> Optional[str]:
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


_DISPUTE_WORDS = ("dispute", "beyond", "underpaid")


class MockLLM:
    """profile='careful' reads the amount from the line that talks about the dispute;
    profile='naive' grabs the first dollar amount anywhere (a classic real-world mistake).
    They exist ONLY to demonstrate the evaluation/compare tooling offline."""

    def __init__(self, profile: str = "careful"):
        if profile not in ("careful", "naive"):
            raise ValueError("profile must be 'careful' or 'naive'")
        self.profile = profile
        self.model_id = f"mock-{profile}"

    def call_tool(self, *, system: str, user_text: str, tool_name: str, tool_description: str = "",
                  schema_model: type[BaseModel] | None = None, max_tokens: int = 0) -> ToolCallResult:
        text = user_text.split("<document>", 1)[-1].split("</document>", 1)[0]
        data = self._classify(text) if tool_name == "record_classification" else self._extract(text, self.profile)
        return ToolCallResult(data, input_tokens=len(user_text) // 4, output_tokens=120,
                              cost_usd=0.0, latency_ms=1, model_id=self.model_id)

    @staticmethod
    def _classify(text: str) -> dict:
        head = text[:600].lower()
        if "notice of intent" in head:
            kind = "idr_registration"
        elif "appeal" in head:
            kind = "provider_appeal"
        elif "complaint" in head:
            kind = "surprise_billing_complaint"
        else:
            kind = "other"
        return {"doc_type": kind, "confidence": 0.95 if kind != "other" else 0.5, "rationale": "keyword match (mock)"}

    @staticmethod
    def _extract(text: str, profile: str = "careful") -> dict:
        def first(rx: re.Pattern) -> Optional[str]:
            m = rx.search(text)
            return m.group(1).strip() if m else None

        amount = first(_AMOUNT)  # naive: first dollar amount anywhere
        if profile == "careful":
            for line in text.splitlines():
                if any(w in line.lower() for w in _DISPUTE_WORDS):
                    m = _AMOUNT.search(line)
                    if m:
                        amount = m.group(1)
                        break
        lowered = text.lower()
        violation = None
        if "surprise billing" in lowered:
            violation = "surprise_billing"
        elif "balance bill" in lowered:
            violation = "balance_billing"
        date_raw = first(_DATE_LINE)
        return {
            "provider_npi": first(_NPI),
            "provider_name": first(_PROVIDER),
            "payer_name": first(_PAYER),
            "claim_reference": first(_CLAIM),
            "service_date": _iso_date(date_raw) if date_raw else None,
            "disputed_amount": float(amount.replace(",", "")) if amount else None,
            "billing_violation_type": violation,
            "summary": "Mock summary (offline stand-in).",
        }


__all__ = ["MockLLM", "PAYER_ALIASES"]
