"""The Phase 1 pipeline: parse -> classify -> extract -> validate.

Analogy: the mail room's assembly line. Each station does one job and either
passes the document along or stops it with a clear reason.
"""
from __future__ import annotations

import logging
from pathlib import Path
from time import perf_counter
from typing import Optional, Protocol

from pydantic import ValidationError

from claimlens.config import Settings, get_settings
from claimlens.llm.bedrock_client import LLMError, ToolCallResult
from claimlens.llm.prompts import load_prompt
from claimlens.models import Classification, DocType, ExtractedClaim, Finding, OcrInfo, ProcessingResult
from claimlens.ocr.base import OcrEngine, OcrError, apply_ocr
from claimlens.parsers.base import ParseError, UnsupportedFileType
from claimlens.parsers.router import parse_file
from claimlens.rules.engine import validate_claim

log = logging.getLogger(__name__)

CLASSIFY_PROMPT = "classify_v1"
EXTRACT_PROMPT = "extract_v1"


class LLM(Protocol):
    model_id: str

    def call_tool(self, **kwargs) -> ToolCallResult: ...


def _wrap(text: str) -> str:
    # Tags make it unmistakable to the model where untrusted content starts and ends.
    return f"<document>\n{text}\n</document>"


def _record(result: ProcessingResult, call: ToolCallResult) -> None:
    result.usage.llm_calls += 1
    result.usage.input_tokens += call.input_tokens
    result.usage.output_tokens += call.output_tokens
    result.usage.cost_usd = round(result.usage.cost_usd + call.cost_usd, 6)


def process_file(
    path: Path,
    llm: LLM,
    settings: Optional[Settings] = None,
    classify_llm: Optional[LLM] = None,
    classify_prompt: str = CLASSIFY_PROMPT,
    extract_prompt: str = EXTRACT_PROMPT,
    ocr: Optional[OcrEngine] = None,
) -> ProcessingResult:
    settings = settings or get_settings()
    classify_llm = classify_llm or llm
    start = perf_counter()
    path = Path(path)
    result = ProcessingResult(
        filename=path.name,
        model_id=getattr(llm, "model_id", None),
        prompt_versions={"classify": classify_prompt, "extract": extract_prompt},
    )

    def finish() -> ProcessingResult:
        result.elapsed_ms = int((perf_counter() - start) * 1000)
        # Only metadata is logged: never document text or extracted values.
        log.info("file=%s status=%s calls=%d cost=$%.5f", result.filename, result.status,
                 result.usage.llm_calls, result.usage.cost_usd)
        return result

    # Station 1: parse
    try:
        parsed = parse_file(path, settings.min_chars_per_page)
    except (UnsupportedFileType, ParseError) as exc:
        result.error, result.error_kind = str(exc), "permanent"
        return finish()
    result.file_type, result.page_count = parsed.file_type, parsed.page_count

    # Station 1b: OCR fallback (only for files with little/no text layer)
    if parsed.any_needs_ocr:
        if ocr is None:
            result.status = "needs_ocr"
            result.findings.append(Finding(rule="ocr_required", severity="info",
                                           message="Little or no text layer and no OCR engine configured."))
            return finish()
        try:
            summary = apply_ocr(parsed, ocr)
        except OcrError as exc:
            result.error = f"OCR failed: {exc}"
            result.error_kind = "transient" if getattr(exc, "transient", False) else "permanent"
            return finish()
        result.usage.ocr_pages = summary.pages
        result.usage.ocr_cost_usd = summary.cost_usd
        result.usage.cost_usd = round(result.usage.cost_usd + summary.cost_usd, 6)
        result.ocr = OcrInfo(engines=summary.engines, pages=summary.pages, mean_confidence=summary.mean_confidence,
                             escalated=summary.escalated, documents=summary.documents)
        if summary.mean_confidence is not None and summary.mean_confidence < settings.min_ocr_confidence:
            result.findings.append(Finding(rule="low_ocr_confidence", severity="soft",
                                           message=f"Mean OCR confidence {summary.mean_confidence:.0f} is below {settings.min_ocr_confidence:.0f}"))

    text = parsed.full_text()
    if not text.strip():
        result.error, result.error_kind = "No text could be extracted from this document.", "permanent"
        return finish()
    if len(text) > settings.max_input_chars:
        text = text[: settings.max_input_chars]
        result.findings.append(Finding(rule="truncated", severity="info",
                                       message=f"Input truncated to {settings.max_input_chars} characters"))

    # Stations 2 & 3 talk to the LLM; anything model-related is handled in one place.
    try:
        # Station 2: classify
        call = classify_llm.call_tool(
            system=load_prompt(classify_prompt), user_text=_wrap(text),
            tool_name="record_classification",
            tool_description="Record the document category and your confidence.",
            schema_model=Classification, max_tokens=300,
        )
        _record(result, call)
        result.classification = Classification(**call.data)

        cls = result.classification
        if cls.doc_type == DocType.OTHER or cls.confidence < settings.min_classification_confidence:
            result.status = "needs_review"
            result.findings.append(Finding(rule="classification", severity="soft",
                                           message="Unrecognized or low-confidence document type"))
            return finish()

        # Station 3: extract
        call = llm.call_tool(
            system=load_prompt(extract_prompt), user_text=_wrap(text),
            tool_name="record_claim",
            tool_description="Record the extracted claim fields. Use null for anything not clearly stated.",
            schema_model=ExtractedClaim, max_tokens=1200,
        )
        _record(result, call)
        extracted = ExtractedClaim(**call.data)
        result.raw_claim = extracted
    except ValidationError as exc:
        # Report WHICH fields failed, never the values (they could contain PHI).
        bad = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        result.error = f"Model output failed schema validation (fields: {', '.join(bad)})"
        result.error_kind = "permanent"
        return finish()
    except LLMError as exc:
        result.error = str(exc)
        result.error_kind = "transient" if exc.transient else "permanent"
        return finish()

    # Station 4: deterministic rules
    claim, findings, status = validate_claim(cls.doc_type, extracted, settings, ocr_used=result.ocr is not None)
    # A shaky OCR read (soft finding raised earlier) can never be auto-accepted, even if the rules pass.
    if status == "validated" and any(f.severity == "soft" for f in result.findings):
        status = "needs_review"
    result.claim, result.status = claim, status  # type: ignore[assignment]
    result.findings.extend(findings)
    return finish()
