"""All database reads/writes live here, so the API and workers never hand-write SQL."""
from __future__ import annotations

import base64
import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from claimlens.config import Settings
from claimlens.eval.metrics import FIELDS
from claimlens.models import DocType, ExtractedClaim, ProcessingResult
from claimlens.rules.engine import REQUIRED_FIELDS, validate_claim
from claimlens.storage.db import AuditLog, Document, ReviewEvent, Upload, utcnow

REVIEWABLE = {"needs_review", "rejected"}
CLAIM_COLUMNS = [f for f in FIELDS]  # the seven scored fields are real columns


class NotFound(Exception):
    pass


class VersionConflict(Exception):
    """Someone else changed this document after you loaded it."""


class ReviewNotAllowed(Exception):
    pass


class CorrectionInvalid(Exception):
    """The proposed correction breaks a rule. `problems` holds rule names and field names, never values."""

    def __init__(self, problems: list[dict]):
        super().__init__("correction rejected by validation")
        self.problems = problems


# ---- writing ------------------------------------------------------------------------------

def save_result(session: Session, result: ProcessingResult, *, content_sha256: str, fingerprint: str,
                source_name: str) -> Document:
    claim = result.claim.model_dump() if result.claim else {}
    doc = Document(
        id=str(uuid.uuid4()), content_sha256=content_sha256, fingerprint=fingerprint, source_name=source_name,
        file_type=result.file_type, page_count=result.page_count, status=result.status,
        doc_type=result.classification.doc_type.value if result.classification else None,
        classification_confidence=result.classification.confidence if result.classification else None,
        provider_npi=claim.get("provider_npi"), provider_name=claim.get("provider_name"),
        payer_name=claim.get("payer_name"), claim_reference=claim.get("claim_reference"),
        service_date=_to_date(claim.get("service_date")), disputed_amount=claim.get("disputed_amount"),
        billing_violation_type=claim.get("billing_violation_type"), summary=claim.get("summary"),
        raw_claim=result.raw_claim.model_dump() if result.raw_claim else None,
        findings=[f.model_dump() for f in result.findings],
        ocr=result.ocr.model_dump() if result.ocr else None,
        prompt_versions=result.prompt_versions, model_id=result.model_id,
        input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
        ocr_pages=result.usage.ocr_pages, cost_usd=result.usage.cost_usd, elapsed_ms=result.elapsed_ms,
        error=result.error, error_kind=result.error_kind,
    )
    session.add(doc)
    session.flush()  # surfaces the unique-constraint violation here, where the caller can handle it
    return doc


def _to_date(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def audit(session: Session, actor: str, action: str, document_id: Optional[str] = None,
          detail: Optional[dict] = None) -> None:
    session.add(AuditLog(actor=actor, action=action, document_id=document_id, detail=detail))


# ---- reading --------------------------------------------------------------------------------

def get_document(session: Session, document_id: str) -> Optional[Document]:
    return session.get(Document, document_id)


def find_by_hash(session: Session, content_sha256: str, fingerprint: str) -> Optional[Document]:
    return session.scalars(select(Document).where(Document.content_sha256 == content_sha256,
                                                  Document.fingerprint == fingerprint)).first()


def _encode_cursor(doc: Document) -> str:
    return base64.urlsafe_b64encode(f"{doc.created_at.isoformat()}|{doc.id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        ts, doc_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(ts), doc_id
    except Exception as exc:
        raise ValueError("invalid cursor") from exc


def list_documents(session: Session, status: Optional[str] = None, limit: int = 50,
                   cursor: Optional[str] = None) -> tuple[list[Document], Optional[str]]:
    """Keyset pagination: 'give me the next page AFTER this exact row'. Unlike OFFSET it stays fast and
    stable on big tables even while new rows arrive."""
    q = select(Document)
    if status:
        q = q.where(Document.status == status)
    if cursor:
        ts, doc_id = _decode_cursor(cursor)
        q = q.where(or_(Document.created_at < ts, and_(Document.created_at == ts, Document.id < doc_id)))
    rows = list(session.scalars(q.order_by(Document.created_at.desc(), Document.id.desc()).limit(limit + 1)))
    next_cursor = _encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


# ---- human review -------------------------------------------------------------------------------

def _current_claim(doc: Document) -> dict:
    return {
        "provider_npi": doc.provider_npi, "provider_name": doc.provider_name, "payer_name": doc.payer_name,
        "claim_reference": doc.claim_reference, "service_date": doc.service_date.isoformat() if doc.service_date else None,
        "disputed_amount": doc.disputed_amount, "billing_violation_type": doc.billing_violation_type,
    }


def apply_review(session: Session, document_id: str, reviewer: str, decision: str, settings: Settings, *,
                 expected_version: int, corrections: Optional[dict[str, Any]] = None,
                 note: Optional[str] = None) -> Document:
    """A human approves (optionally fixing fields) or rejects a document.

    Humans may override *soft* concerns (e.g. 'please confirm the date is really absent') but not
    provably-invalid data (bad NPI check digit, future date) and may not approve with required fields empty.
    """
    try:
        doc = session.get(Document, document_id, with_for_update=True)
    except StaleDataError as exc:  # our in-memory copy is older than the row we just re-read
        session.rollback()
        raise VersionConflict("document was modified concurrently") from exc
    if doc is None:
        raise NotFound(document_id)
    if doc.version != expected_version:
        raise VersionConflict(f"document is at version {doc.version}, you edited version {expected_version}")
    if doc.status not in REVIEWABLE:
        raise ReviewNotAllowed(f"documents with status '{doc.status}' cannot be reviewed")

    prior = doc.status
    changes: dict[str, list] = {}

    if decision == "reject":
        if not note or len(note.strip()) < 3:
            raise CorrectionInvalid([{"field": "note", "rule": "note_required_when_rejecting"}])
        doc.status = "rejected"
    elif decision == "approve":
        corrections = dict(corrections or {})
        new_type = corrections.pop("doc_type", None)
        unknown = [k for k in corrections if k not in CLAIM_COLUMNS]
        if unknown:
            raise CorrectionInvalid([{"field": k, "rule": "unknown_field"} for k in unknown])
        if new_type is not None:
            try:
                doc_type = DocType(new_type)
            except ValueError:
                raise CorrectionInvalid([{"field": "doc_type", "rule": "invalid_doc_type"}])
            if doc_type == DocType.OTHER:
                raise CorrectionInvalid([{"field": "doc_type", "rule": "cannot_approve_as_other"}])
        else:
            try:
                doc_type = DocType(doc.doc_type) if doc.doc_type else DocType.OTHER
            except ValueError:
                doc_type = DocType.OTHER
        if doc_type == DocType.OTHER:
            raise CorrectionInvalid([{"field": "doc_type", "rule": "doc_type_required"}])

        merged = {**_current_claim(doc), **corrections}
        try:
            claim = ExtractedClaim(**merged)
        except Exception as exc:  # pydantic ValidationError: report field names only
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in getattr(exc, "errors", lambda: [])()}) or ["claim"]
            raise CorrectionInvalid([{"field": f, "rule": "invalid_value"} for f in fields])

        cleaned, findings, _ = validate_claim(doc_type, claim, settings, ocr_used=bool(doc.ocr))
        blocking = [f for f in findings if f.severity == "hard" or f.rule == "required_field"]
        if blocking:
            raise CorrectionInvalid([{"rule": f.rule, "message": f.message} for f in blocking])

        before, after = _current_claim(doc), cleaned.model_dump()
        for f in CLAIM_COLUMNS:
            if before.get(f) != after.get(f):
                changes[f] = [before.get(f), after.get(f)]
                setattr(doc, f, _to_date(after[f]) if f == "service_date" else after[f])
        if new_type is not None and doc.doc_type != doc_type.value:
            changes["doc_type"] = [doc.doc_type, doc_type.value]
            doc.doc_type = doc_type.value
        doc.status = "validated"
    else:
        raise CorrectionInvalid([{"field": "decision", "rule": "must_be_approve_or_reject"}])

    doc.reviewed_by, doc.reviewed_at = reviewer, utcnow()
    session.add(ReviewEvent(document_id=doc.id, reviewer=reviewer, decision=decision, prior_status=prior,
                            new_status=doc.status, note=note, changes=changes or None))
    audit(session, reviewer, f"review_{decision}", doc.id, {"changed_fields": sorted(changes)})
    try:
        session.flush()  # raises StaleDataError if another writer slipped in (optimistic lock)
    except StaleDataError as exc:
        session.rollback()
        raise VersionConflict("document was modified concurrently") from exc
    return doc


def export_ground_truth(session: Session, out_dir: Path) -> int:
    """Turn human-approved documents into answer keys for the Phase 2 evaluator.

    The loop that makes the system improve: model is wrong -> human fixes -> the fix becomes a test case.
    NOTE: this writes ONLY the answer keys. Copy the original files next to them to evaluate (Phase 5 will
    keep originals in S3).
    """
    truth_dir = Path(out_dir) / "ground_truth"
    truth_dir.mkdir(parents=True, exist_ok=True)
    # EXISTS (not JOIN + DISTINCT): Postgres cannot compare json columns for equality, so DISTINCT over whole rows fails.
    approved = exists().where(ReviewEvent.document_id == Document.id, ReviewEvent.decision == "approve")
    docs = session.scalars(select(Document).where(approved, Document.status == "validated")).all()
    for d in docs:
        payload = {"filename": d.source_name, "doc_type": d.doc_type, "difficulty": "human_reviewed",
                   "injected": [], "scan_pages": 0, "expected": _current_claim(d)}
        (truth_dir / f"{d.id[:8]}_{d.doc_type}.json").write_text(json.dumps(payload, indent=2))
    return len(docs)


def document_to_dict(d: Document) -> dict:
    """Plain dict for API responses (never includes document text; we never stored any)."""
    claim = _current_claim(d) if d.status not in ("error", "needs_ocr") or d.provider_npi or d.payer_name else None
    return {
        "id": d.id, "source_name": d.source_name, "content_sha256": d.content_sha256, "status": d.status,
        "doc_type": d.doc_type, "classification_confidence": d.classification_confidence,
        "file_type": d.file_type, "page_count": d.page_count, "claim": claim,
        "findings": d.findings or [], "ocr": d.ocr,
        "usage": {"input_tokens": d.input_tokens, "output_tokens": d.output_tokens, "ocr_pages": d.ocr_pages,
                  "cost_usd": d.cost_usd},
        "model_id": d.model_id, "prompt_versions": d.prompt_versions,
        "error": d.error, "error_kind": d.error_kind, "version": d.version,
        "created_at": d.created_at, "updated_at": d.updated_at,
        "reviewed_by": d.reviewed_by, "reviewed_at": d.reviewed_at,
    }


# ---- async uploads --------------------------------------------------------------------------------

def create_upload(session: Session, upload_id: str, key: str, filename: str, requested_by: str) -> Upload:
    row = Upload(id=upload_id, s3_key=key, filename=filename, requested_by=requested_by, state="pending")
    session.add(row)
    session.flush()
    return row


def get_upload(session: Session, upload_id: str) -> Optional[Upload]:
    return session.get(Upload, upload_id)


def mark_upload(session: Session, upload_id: str, state: str, document_id: Optional[str] = None,
                error: Optional[str] = None) -> bool:
    """Called by the worker. Returns False if there is no such upload (files dropped into S3 by other
    means have no row, which is fine)."""
    row = session.get(Upload, upload_id)
    if row is None:
        return False
    row.state, row.document_id, row.error = state, document_id, (error or None)
    session.flush()
    return True
