from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ClaimOut(BaseModel):
    provider_npi: Optional[str] = None
    provider_name: Optional[str] = None
    payer_name: Optional[str] = None
    claim_reference: Optional[str] = None
    service_date: Optional[str] = None
    disputed_amount: Optional[float] = None
    billing_violation_type: Optional[str] = None


class UsageOut(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    ocr_pages: int = 0
    cost_usd: float = 0.0


class DocumentOut(BaseModel):
    id: str
    source_name: str
    content_sha256: str
    status: str
    doc_type: Optional[str] = None
    classification_confidence: Optional[float] = None
    file_type: Optional[str] = None
    page_count: Optional[int] = None
    claim: Optional[ClaimOut] = None
    findings: list[dict[str, Any]] = Field(default_factory=list)
    ocr: Optional[dict[str, Any]] = None
    usage: UsageOut
    model_id: Optional[str] = None
    prompt_versions: Optional[dict[str, str]] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None
    version: int
    created_at: datetime
    updated_at: datetime
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None


class IngestResponse(BaseModel):
    outcome: Literal["created", "duplicate", "in_progress"]
    document: Optional[DocumentOut] = None
    detail: Optional[str] = None


class DocumentList(BaseModel):
    items: list[DocumentOut]
    next_cursor: Optional[str] = None


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    expected_version: int = Field(ge=1, description="the `version` you loaded; protects against overwriting a colleague's change")
    corrections: dict[str, Any] = Field(default_factory=dict)
    note: Optional[str] = Field(default=None, max_length=1000)
