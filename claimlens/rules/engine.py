"""Deterministic rules: the 'supervisor with a checklist' that double-checks the LLM.

Severity meanings (these drive the final status):
  hard -> the value is provably wrong/invalid  -> status "rejected"
  soft -> something is missing or unusual, a human should look -> "needs_review"
  info -> FYI only
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from claimlens.config import Settings
from claimlens.models import DocType, ExtractedClaim, Finding
from claimlens.rules.npi import is_valid_npi
from claimlens.rules.payers import normalize_payer

# Bump when ANY rule changes: stored results are tagged with this so old verdicts can be told apart.
RULES_VERSION = "2"

REQUIRED_FIELDS: dict[DocType, list[str]] = {
    DocType.SURPRISE_BILLING_COMPLAINT: [
        "provider_npi", "payer_name", "disputed_amount", "service_date", "billing_violation_type",
    ],
    DocType.PROVIDER_APPEAL: ["provider_npi", "payer_name", "disputed_amount", "claim_reference"],
    DocType.IDR_REGISTRATION: ["provider_npi", "provider_name", "payer_name"],
    DocType.OTHER: [],
}


# Fields we normally expect on these documents. If the text came from OCR, a *missing* one is more likely
# "OCR dropped it" than "the document never had it", so a human should confirm.
OCR_EXPECTED_FIELDS = ["provider_npi", "provider_name", "payer_name", "claim_reference", "service_date", "disputed_amount"]

# Letters/digits plus normal business-name punctuation. OCR garbage (em dashes, underscores, pipes) fails this.
_NAME_OK = re.compile(r"^(?:[^\W_]|[&'.,\-/ ()])+$")


def _parse_iso(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def validate_claim(
    doc_type: DocType,
    claim: ExtractedClaim,
    settings: Settings,
    today: Optional[date] = None,
    ocr_used: bool = False,
) -> tuple[ExtractedClaim, list[Finding], str]:
    """Return (cleaned claim, findings, status)."""
    today = today or date.today()
    findings: list[Finding] = []
    updates: dict = {}

    # 1. Required fields for this document type
    for field in REQUIRED_FIELDS[doc_type]:
        if getattr(claim, field) in (None, ""):
            findings.append(Finding(rule="required_field", severity="soft", message=f"Missing required field: {field}"))

    # 1b. Stricter completeness when the text was recovered by OCR
    if ocr_used:
        already = {f.message.split(": ")[-1] for f in findings if f.rule == "required_field"}
        for field in OCR_EXPECTED_FIELDS:
            if getattr(claim, field) in (None, "") and field not in already:
                findings.append(Finding(rule="ocr_completeness", severity="soft",
                                        message=f"Field not found in OCR text, please confirm: {field}"))

    # 2. NPI format + Luhn check digit
    if claim.provider_npi:
        digits = "".join(ch for ch in claim.provider_npi if ch.isdigit())
        if not is_valid_npi(digits):
            findings.append(Finding(rule="npi_valid", severity="hard", message="provider_npi failed format/check-digit validation"))
        else:
            updates["provider_npi"] = digits

    # 2b. Name plausibility (names have no check digit, so we can at least reject obvious garbage)
    if claim.provider_name and not _NAME_OK.match(claim.provider_name.strip()):
        findings.append(Finding(rule="name_plausible", severity="soft", message="provider_name contains unexpected characters"))

    # 3. Amount sanity
    if claim.disputed_amount is not None:
        if claim.disputed_amount <= 0:
            findings.append(Finding(rule="amount_positive", severity="hard", message="disputed_amount must be > 0"))
        elif claim.disputed_amount > settings.max_disputed_amount:
            findings.append(Finding(rule="amount_ceiling", severity="soft", message="disputed_amount exceeds configured ceiling"))

    # 4. Service date must parse and not be in the future
    if claim.service_date:
        parsed = _parse_iso(claim.service_date)
        if parsed is None:
            findings.append(Finding(rule="date_format", severity="hard", message="service_date is not YYYY-MM-DD"))
        elif parsed > today:
            findings.append(Finding(rule="date_not_future", severity="hard", message="service_date is in the future"))

    # 5. Payer normalization
    if claim.payer_name:
        canonical = normalize_payer(claim.payer_name)
        if canonical is None:
            findings.append(Finding(rule="payer_known", severity="soft", message="payer_name not in the known payer list"))
        elif canonical != claim.payer_name:
            updates["payer_name"] = canonical
            findings.append(Finding(rule="payer_normalized", severity="info", message="payer_name normalized to canonical form"))

    if any(f.severity == "hard" for f in findings):
        status = "rejected"
    elif any(f.severity == "soft" for f in findings):
        status = "needs_review"
    else:
        status = "validated"

    return claim.model_copy(update=updates), findings, status
