"""Run the pipeline over a labelled folder and grade it.

Analogy: the exam proctor. Hand every document to the pipeline, collect the
answer sheets, grade them against the key, and write the report card.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from claimlens.config import Settings, get_settings
from claimlens.eval.metrics import FIELDS, GOOD_OUTCOMES, Counts, percentile, score_document
from claimlens.llm.bedrock_client import BedrockClient
from claimlens.llm.mock import MockLLM
from claimlens.models import ProcessingResult
from claimlens.ocr.factory import build_ocr
from claimlens.pipeline import CLASSIFY_PROMPT, EXTRACT_PROMPT, process_file


# ---- configuration of ONE experiment ----------------------------------------

@dataclass
class RunConfig:
    """Everything that can differ between two runs: this is what you A/B test."""
    label: str = "default"
    llm: str = "bedrock"            # 'bedrock' | 'mock:careful' | 'mock:naive'
    model_id: str = ""              # extraction model (bedrock only; falls back to CLAIMLENS_MODEL_ID)
    classify_model_id: str = ""     # classification model (falls back to model_id)
    classify_prompt: str = CLASSIFY_PROMPT
    extract_prompt: str = EXTRACT_PROMPT
    ocr: str = "none"               # none | tesseract | textract | tesseract+textract | oracle[:error_rate]

    def describe(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v != ""}


def build_ocr_for(cfg: RunConfig, settings: Settings, data_dir: Path):
    """Oracle OCR reads the answer text saved next to the generated data; the others are real engines."""
    return build_ocr(cfg.ocr, settings, oracle_dir=Path(data_dir) / "ocr_oracle")


def parse_config(spec: str) -> RunConfig:
    """'label=v2,llm=bedrock,extract_prompt=extract_v2,model_id=abc:0' -> RunConfig"""
    cfg = RunConfig()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "=" not in part:
            raise ValueError(f"Bad config piece '{part}' (expected key=value)")
        key, value = part.split("=", 1)
        if key not in cfg.__dict__:
            raise ValueError(f"Unknown config key '{key}'. Valid keys: {', '.join(cfg.__dict__)}")
        setattr(cfg, key, value.strip())
    return cfg


def build_llms(cfg: RunConfig, settings: Settings):
    if cfg.llm.startswith("mock"):
        profile = cfg.llm.split(":", 1)[1] if ":" in cfg.llm else "careful"
        mock = MockLLM(profile)
        return mock, mock
    if cfg.llm != "bedrock":
        raise ValueError(f"Unknown llm '{cfg.llm}' (use bedrock, mock:careful or mock:naive)")
    model = cfg.model_id or settings.model_id
    classify = cfg.classify_model_id or settings.classify_model_id or model
    return BedrockClient(model, settings.region), BedrockClient(classify, settings.region)


# ---- report shapes -------------------------------------------------------------

class FieldStats(BaseModel):
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    precision: Optional[float] = None
    recall: Optional[float] = None
    f1: Optional[float] = None

    @classmethod
    def from_counts(cls, c: Counts) -> "FieldStats":
        return cls(tp=c.tp, fp=c.fp, fn=c.fn, tn=c.tn, precision=c.precision, recall=c.recall, f1=c.f1)


class ErrorItem(BaseModel):
    filename: str
    doc_type: str
    field: str
    kind: str                    # wrong_value | missed | hallucinated | classification
    injected: list[str] = Field(default_factory=list)
    status: str
    auto_accepted: bool          # True = the pipeline said "validated" despite this error (the dangerous kind)
    expected: Optional[str] = None  # only filled with --show-values
    got: Optional[str] = None


class DocOutcome(BaseModel):
    filename: str
    doc_type: str
    status: str
    exact_match: bool
    wrong_fields: list[str]
    scanned: bool = False
    cost_usd: float
    latency_ms: int


class EvalReport(BaseModel):
    label: str
    config: dict
    n_docs: int
    fields: dict[str, FieldStats]
    micro: FieldStats
    raw_micro: Optional[FieldStats] = None     # model output BEFORE the rules engine
    classification_accuracy: float
    confusion: dict[str, dict[str, int]]
    exact_match_rate: float
    status_counts: dict[str, int]
    n_auto_accepted: int
    auto_accept_precision: Optional[float]     # of docs marked 'validated', how many were truly perfect?
    n_docs_with_errors: int
    error_catch_rate: Optional[float]          # of docs with any error, how many did the pipeline refuse to auto-accept?
    total_cost_usd: float
    cost_per_doc: float
    cost_per_exact_doc: Optional[float]
    avg_input_tokens: float
    avg_output_tokens: float
    latency_p50_ms: float
    latency_p95_ms: float
    # OCR section
    n_scanned: int = 0
    exact_match_rate_scanned: Optional[float] = None
    exact_match_rate_native: Optional[float] = None
    n_ocr_docs: int = 0
    ocr_pages: int = 0
    ocr_cost_usd: float = 0.0
    ocr_escalations: int = 0
    ocr_mean_confidence: Optional[float] = None
    n_needs_ocr: int = 0
    errors: list[ErrorItem]
    error_summary: dict[str, int]
    docs: list[DocOutcome]


# ---- the evaluation ---------------------------------------------------------------

def load_ground_truth(data_dir: Path) -> list[dict]:
    truth_dir = Path(data_dir) / "ground_truth"
    items = [json.loads(p.read_text()) for p in sorted(truth_dir.glob("*.json"))]
    if not items:
        raise ValueError(f"No ground-truth files found in {truth_dir}. Run `gen-data` first.")
    return items


def evaluate(
    data_dir: Path,
    cfg: RunConfig,
    llm,
    classify_llm=None,
    settings: Optional[Settings] = None,
    workers: int = 4,
    show_values: bool = False,
    ocr=None,
) -> EvalReport:
    settings = settings or get_settings()
    data_dir = Path(data_dir)
    truths = load_ground_truth(data_dir)

    def run_one(truth: dict) -> ProcessingResult:
        return process_file(data_dir / truth["filename"], llm, settings, classify_llm,
                            classify_prompt=cfg.classify_prompt, extract_prompt=cfg.extract_prompt, ocr=ocr)

    # Threads suit this I/O-bound work (each call mostly waits on the network).
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(run_one, truths))

    field_counts = {f: Counts() for f in FIELDS}
    micro, raw_micro = Counts(), Counts()
    confusion: dict[str, dict[str, int]] = {}
    status_counts: dict[str, int] = {}
    errors: list[ErrorItem] = []
    docs: list[DocOutcome] = []
    cls_correct = exact = auto_accepted = auto_accepted_ok = docs_with_errors = caught = 0
    scanned_n = scanned_exact = native_exact = 0

    for truth, res in zip(truths, results):
        expected, doc_type = truth["expected"], truth["doc_type"]
        predicted = res.claim.model_dump() if res.claim else None
        outcomes = score_document(expected, predicted)
        raw_outcomes = score_document(expected, res.raw_claim.model_dump() if res.raw_claim else None)

        for f, o in outcomes.items():
            field_counts[f].add(o)
            micro.add(o)
        for o in raw_outcomes.values():
            raw_micro.add(o)

        got_type = res.classification.doc_type.value if res.classification else "none"
        confusion.setdefault(doc_type, {}).setdefault(got_type, 0)
        confusion[doc_type][got_type] += 1
        type_ok = got_type == doc_type
        cls_correct += type_ok

        wrong = [f for f, o in outcomes.items() if o not in GOOD_OUTCOMES]
        if not type_ok:
            wrong.append("doc_type")
        is_exact = not wrong
        exact += is_exact
        is_scanned = "scanned" in truth.get("injected", [])
        scanned_n += is_scanned
        scanned_exact += is_scanned and is_exact
        native_exact += (not is_scanned) and is_exact
        status_counts[res.status] = status_counts.get(res.status, 0) + 1
        is_auto = res.status == "validated"
        if is_auto:
            auto_accepted += 1
            auto_accepted_ok += is_exact
        if not is_exact:
            docs_with_errors += 1
            caught += (not is_auto)

        for f in wrong:
            kind = "classification" if f == "doc_type" else outcomes[f]
            errors.append(ErrorItem(
                filename=truth["filename"], doc_type=doc_type, field=f, kind=kind,
                injected=truth.get("injected", []), status=res.status, auto_accepted=is_auto,
                expected=(str(expected.get(f)) if show_values and f != "doc_type" else None),
                got=(str(predicted.get(f)) if show_values and predicted and f != "doc_type" else None),
            ))
        docs.append(DocOutcome(filename=truth["filename"], doc_type=doc_type, status=res.status,
                               exact_match=is_exact, wrong_fields=wrong, scanned=is_scanned, cost_usd=res.usage.cost_usd,
                               latency_ms=res.elapsed_ms))

    n = len(truths)
    ocr_results = [r for r in results if r.ocr]
    confs = [r.ocr.mean_confidence for r in ocr_results if r.ocr.mean_confidence is not None]
    total_cost = round(sum(r.usage.cost_usd for r in results), 6)
    summary: dict[str, int] = {}
    for e in errors:
        summary[f"{e.field}:{e.kind}"] = summary.get(f"{e.field}:{e.kind}", 0) + 1

    return EvalReport(
        label=cfg.label, config=cfg.describe(), n_docs=n,
        fields={f: FieldStats.from_counts(c) for f, c in field_counts.items()},
        micro=FieldStats.from_counts(micro), raw_micro=FieldStats.from_counts(raw_micro),
        classification_accuracy=cls_correct / n, confusion=confusion,
        exact_match_rate=exact / n, status_counts=status_counts,
        n_auto_accepted=auto_accepted,
        auto_accept_precision=(auto_accepted_ok / auto_accepted) if auto_accepted else None,
        n_docs_with_errors=docs_with_errors,
        error_catch_rate=(caught / docs_with_errors) if docs_with_errors else None,
        total_cost_usd=total_cost, cost_per_doc=round(total_cost / n, 6),
        cost_per_exact_doc=round(total_cost / exact, 6) if exact else None,
        avg_input_tokens=sum(r.usage.input_tokens for r in results) / n,
        avg_output_tokens=sum(r.usage.output_tokens for r in results) / n,
        latency_p50_ms=percentile([r.elapsed_ms for r in results], 50),
        latency_p95_ms=percentile([r.elapsed_ms for r in results], 95),
        n_scanned=scanned_n,
        exact_match_rate_scanned=(scanned_exact / scanned_n) if scanned_n else None,
        exact_match_rate_native=(native_exact / (n - scanned_n)) if n - scanned_n else None,
        n_ocr_docs=len(ocr_results), ocr_pages=sum(r.usage.ocr_pages for r in results),
        ocr_cost_usd=round(sum(r.usage.ocr_cost_usd for r in results), 6),
        ocr_escalations=sum(1 for r in ocr_results if r.ocr.escalated),
        ocr_mean_confidence=(sum(confs) / len(confs)) if confs else None,
        n_needs_ocr=status_counts.get("needs_ocr", 0),
        errors=errors, error_summary=dict(sorted(summary.items(), key=lambda kv: -kv[1])), docs=docs,
    )
