"""Data shapes (Pydantic models).

Analogy: these are the blank forms. A Pydantic model says "this form has these
boxes, and each box accepts only this kind of answer." If an answer doesn't fit
the box, Pydantic refuses it loudly instead of letting bad data sneak downstream.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class DocType(str, Enum):
    SURPRISE_BILLING_COMPLAINT = "surprise_billing_complaint"
    PROVIDER_APPEAL = "provider_appeal"
    IDR_REGISTRATION = "idr_registration"
    OTHER = "other"


BillingViolation = Literal["surprise_billing", "balance_billing", "good_faith_estimate", "other"]


class Classification(BaseModel):
    """What the model must return from the classification step."""

    doc_type: DocType
    confidence: float = Field(ge=0.0, le=1.0, description="0 = guessing, 1 = certain")
    rationale: str = Field(description="One short sentence. Do NOT include names or personal identifiers.")


class ExtractedClaim(BaseModel):
    """What the model must return from the extraction step.

    Every field is Optional on purpose: "I couldn't find it" must be an allowed
    answer, otherwise we are forcing the model to invent values.
    """

    provider_npi: Optional[str] = Field(
        default=None, description="10-digit National Provider Identifier of the billing provider, digits only."
    )
    provider_name: Optional[str] = Field(default=None, description="Name of the billing provider or facility.")
    payer_name: Optional[str] = Field(default=None, description="Health plan / insurer name as written.")
    claim_reference: Optional[str] = Field(default=None, description="Claim or reference number as written.")
    service_date: Optional[str] = Field(default=None, description="Date of service in YYYY-MM-DD format.")
    disputed_amount: Optional[float] = Field(
        default=None, description="Dollar amount in dispute as a plain number (no currency symbol or commas)."
    )
    billing_violation_type: Optional[BillingViolation] = Field(
        default=None, description="Only if the document clearly states the type of billing violation."
    )
    summary: Optional[str] = Field(
        default=None, description="1-2 sentence neutral summary. No patient names or identifiers."
    )


class Finding(BaseModel):
    """One result from the rules engine."""

    rule: str
    severity: Literal["hard", "soft", "info"]
    message: str


class Usage(BaseModel):
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    ocr_pages: int = 0
    ocr_cost_usd: float = 0.0
    cost_usd: float = 0.0  # TOTAL (LLM + OCR)


class OcrInfo(BaseModel):
    engines: list[str]               # e.g. ['tesseract'] or ['tesseract', 'textract'] if it escalated
    pages: int
    mean_confidence: float | None = None
    escalated: bool = False
    documents: int = 1               # how many files/attachments needed OCR


Status = Literal["validated", "needs_review", "rejected", "needs_ocr", "error"]


class ProcessingResult(BaseModel):
    filename: str
    status: Status = "error"
    file_type: Optional[str] = None
    page_count: Optional[int] = None
    classification: Optional[Classification] = None
    ocr: Optional[OcrInfo] = None
    claim: Optional[ExtractedClaim] = None      # after rules (normalized)
    raw_claim: Optional[ExtractedClaim] = None  # exactly what the model said
    findings: list[Finding] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    model_id: Optional[str] = None
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    elapsed_ms: int = 0
    error: Optional[str] = None
    error_kind: Optional[Literal["transient", "permanent"]] = None  # transient = safe to retry later
