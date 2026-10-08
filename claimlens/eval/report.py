"""Turn EvalReports into readable markdown (single report and A/B comparison)."""
from __future__ import annotations

from typing import Optional

from claimlens.eval.runner import EvalReport

SMALL_SAMPLE = 30

# Which injected difficulty could plausibly cause an error in which field.
LIKELY_CAUSE = {
    "provider_npi": "npi_ocr_error",
    "disputed_amount": "distractor_amount",
    "service_date": "service_date_absent",
}


def likely_cause(error) -> str:
    cause = LIKELY_CAUSE.get(error.field)
    if cause and cause in error.injected:
        return cause
    return "ocr_quality" if "scanned" in error.injected else "unexplained"


def _p(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:.3f}"


def _pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def _usd(x: Optional[float]) -> str:
    return "-" if x is None else f"${x:.5f}"


def render_report(r: EvalReport, max_errors: int = 15) -> str:
    out = [f"# Evaluation: {r.label}", "", f"Config: `{r.config}`  |  Documents scored: **{r.n_docs}**", ""]
    if r.n_docs < SMALL_SAMPLE:
        out += [f"> Small sample (n={r.n_docs}): treat differences of a few points as noise. Aim for 50-100+ documents.", ""]

    out += ["## Headline", "", "| metric | value | what it answers |", "|---|---|---|",
            f"| Micro F1 (all fields) | {_p(r.micro.f1)} | Overall extraction quality |",
            f"| Exact-match documents | {_pct(r.exact_match_rate)} | Every field AND the doc type right |",
            f"| Classification accuracy | {_pct(r.classification_accuracy)} | Did we route to the right document type? |",
            f"| **Auto-accept precision** | {_pct(r.auto_accept_precision)} | Of docs marked `validated`, how many were truly perfect? (want ~100%) |",
            f"| **Error catch rate** | {_pct(r.error_catch_rate)} | Of docs with ANY error, how many did we refuse to auto-accept? |",
            f"| Docs auto-accepted | {r.n_auto_accepted}/{r.n_docs} | Work that needs no human |", ""]

    if r.raw_micro:
        out += [f"Rules engine effect: micro F1 **{_p(r.raw_micro.f1)}** (raw model output) -> **{_p(r.micro.f1)}** (after rules/normalization).", ""]

    out += ["## Per-field scores", "", "| field | TP | FP | FN | precision | recall | F1 |", "|---|---|---|---|---|---|---|"]
    for name, s in r.fields.items():
        out.append(f"| {name} | {s.tp} | {s.fp} | {s.fn} | {_p(s.precision)} | {_p(s.recall)} | {_p(s.f1)} |")
    out.append(f"| **micro (all)** | {r.micro.tp} | {r.micro.fp} | {r.micro.fn} | {_p(r.micro.precision)} | {_p(r.micro.recall)} | {_p(r.micro.f1)} |")

    preds = sorted({p for row in r.confusion.values() for p in row})
    out += ["", "## Classification confusion (rows = truth, columns = predicted)", "",
            "| truth \\ predicted | " + " | ".join(preds) + " |", "|---|" + "---|" * len(preds)]
    for truth, row in sorted(r.confusion.items()):
        out.append(f"| {truth} | " + " | ".join(str(row.get(p, 0)) for p in preds) + " |")

    out += ["", "## Pipeline decisions", "", "| status | count |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in sorted(r.status_counts.items())]

    if r.n_scanned or r.n_ocr_docs or r.n_needs_ocr:
        out += ["", "## OCR", "", "| metric | value |", "|---|---|",
                f"| Scanned documents in the set | {r.n_scanned} |",
                f"| Documents that went through OCR | {r.n_ocr_docs} ({r.ocr_pages} pages) |",
                f"| Stopped at `needs_ocr` (no engine configured) | {r.n_needs_ocr} |",
                f"| OCR mean confidence | {'-' if r.ocr_mean_confidence is None else f'{r.ocr_mean_confidence:.1f}'} |",
                f"| Escalated to the expensive engine | {r.ocr_escalations} |",
                f"| OCR cost | {_usd(r.ocr_cost_usd)} |",
                f"| Exact-match: scanned docs | {_pct(r.exact_match_rate_scanned)} |",
                f"| Exact-match: native-text docs | {_pct(r.exact_match_rate_native)} |"]

    out += ["", "## Cost & latency", "", "| metric | value |", "|---|---|",
            f"| Total cost | {_usd(r.total_cost_usd)} |", f"| Cost per document | {_usd(r.cost_per_doc)} |",
            f"| Cost per *perfect* document | {_usd(r.cost_per_exact_doc)} |",
            f"| Avg tokens in / out | {r.avg_input_tokens:.0f} / {r.avg_output_tokens:.0f} |",
            f"| Latency p50 / p95 | {r.latency_p50_ms:.0f} ms / {r.latency_p95_ms:.0f} ms |"]
    if r.total_cost_usd == 0:
        out += ["", "> Cost is $0.00 (mock run or placeholder pricing). Real costs need a real model and current prices in `pricing.json`."]

    out += ["", "## Error analysis", ""]
    if not r.errors:
        out += ["No errors. (Suspiciously perfect? Try `gen-data --difficulty hard`.)"]
    else:
        out += ["Most common failure types (field:kind):", ""]
        out += [f"- `{k}`: {v}" for k, v in list(r.error_summary.items())[:10]]
        dangerous = [e for e in r.errors if e.auto_accepted]
        out += ["", f"**False accepts** (wrong data the pipeline marked `validated`): {len(dangerous)}"]
        by_cause: dict[str, int] = {}
        for e in r.errors:
            c = likely_cause(e)
            by_cause[c] = by_cause.get(c, 0) + 1
        out += ["", "Errors by likely cause (matched to injected difficulties in hard-mode data):", ""]
        out += [f"- `{k}`: {v}" for k, v in sorted(by_cause.items(), key=lambda kv: -kv[1])]
        out += ["", "_`unexplained` = an error with no matching injected difficulty. Those are the real model/prompt bugs to dig into first._"]
        out += ["", f"First {min(max_errors, len(r.errors))} errors (dangerous ones first):", "",
                "| file | field | kind | status | injected |" + (" expected | got |" if r.errors[0].expected is not None else ""),
                "|---|---|---|---|---|" + ("---|---|" if r.errors[0].expected is not None else "")]
        for e in sorted(r.errors, key=lambda e: (not e.auto_accepted, e.filename))[:max_errors]:
            row = f"| {e.filename} | {e.field} | {e.kind} | {e.status} | {', '.join(e.injected) or '-'} |"
            if e.expected is not None or e.got is not None:
                row += f" {e.expected} | {e.got} |"
            out.append(row)
        if r.errors[0].expected is None:
            out += ["", "_Values are hidden by default (they could be PHI on real data). Use `--show-values` on synthetic data only._"]
    return "\n".join(out) + "\n"


def render_compare(a: EvalReport, b: EvalReport) -> str:
    def row(name, va, vb, fmt, higher_better=True):
        if va is None or vb is None:
            return f"| {name} | {fmt(va)} | {fmt(vb)} | - |"
        d = vb - va
        mark = "" if abs(d) < 1e-9 else (" (better)" if (d > 0) == higher_better else " (worse)")
        return f"| {name} | {fmt(va)} | {fmt(vb)} | {d:+.3f}{mark} |"

    out = [f"# A/B comparison: {a.label} (A) vs {b.label} (B)", "",
           f"A: `{a.config}`", f"B: `{b.config}`", f"Documents: {a.n_docs} (A) / {b.n_docs} (B)", ""]
    if min(a.n_docs, b.n_docs) < SMALL_SAMPLE:
        out += [f"> Small sample (n<{SMALL_SAMPLE}): a difference of a couple of documents can be luck. Grow the eval set before trusting a close result.", ""]

    out += ["## Headline metrics", "", "| metric | A | B | delta |", "|---|---|---|---|",
            row("Micro F1", a.micro.f1, b.micro.f1, _p),
            row("Micro precision", a.micro.precision, b.micro.precision, _p),
            row("Micro recall", a.micro.recall, b.micro.recall, _p),
            row("Exact-match docs", a.exact_match_rate, b.exact_match_rate, _p),
            row("Auto-accept precision", a.auto_accept_precision, b.auto_accept_precision, _p),
            row("Error catch rate", a.error_catch_rate, b.error_catch_rate, _p),
            row("Docs auto-accepted", a.n_auto_accepted, b.n_auto_accepted, lambda x: str(x)),
            row("Exact-match: scanned docs", a.exact_match_rate_scanned, b.exact_match_rate_scanned, _p),
            row("OCR cost (USD)", a.ocr_cost_usd, b.ocr_cost_usd, _usd, higher_better=False),
            row("OCR escalations", a.ocr_escalations, b.ocr_escalations, lambda x: str(x), higher_better=False),
            row("Cost per doc (USD)", a.cost_per_doc, b.cost_per_doc, _usd, higher_better=False),
            row("Latency p95 (ms)", a.latency_p95_ms, b.latency_p95_ms, lambda x: f"{x:.0f}", higher_better=False), ""]

    out += ["## Per-field F1", "", "| field | A | B | delta |", "|---|---|---|---|"]
    for f in a.fields:
        out.append(row(f, a.fields[f].f1, b.fields[f].f1, _p))

    da, db = {d.filename: d for d in a.docs}, {d.filename: d for d in b.docs}
    shared = sorted(set(da) & set(db))
    improved = [f for f in shared if db[f].exact_match and not da[f].exact_match]
    regressed = [f for f in shared if da[f].exact_match and not db[f].exact_match]
    out += ["", "## Document-level head-to-head", "",
            f"- B fixed **{len(improved)}** documents A got wrong" + (f": {', '.join(improved[:8])}" if improved else ""),
            f"- B broke **{len(regressed)}** documents A got right" + (f": {', '.join(regressed[:8])}" if regressed else ""),
            f"- Unchanged: {len(shared) - len(improved) - len(regressed)}", ""]
    if regressed:
        out += ["> Always inspect regressions: an overall gain can hide a new failure on an important document type.", ""]

    verdict = "No clear difference."
    if b.micro.f1 is not None and a.micro.f1 is not None:
        if len(improved) > len(regressed) and b.micro.f1 >= a.micro.f1:
            verdict = f"B looks better ({len(improved)} fixed vs {len(regressed)} broken)."
        elif len(regressed) > len(improved) and a.micro.f1 >= b.micro.f1:
            verdict = f"A looks better ({len(regressed)} fixed-by-A vs {len(improved)} for B)."
        elif len(improved) == len(regressed):
            verdict = "Documents fixed and broken are balanced: no clear winner."
        else:
            verdict = "Mixed signals: field metrics and document counts disagree. Look at the details."
    out += [f"**Verdict:** {verdict}", ""]
    return "\n".join(out)
