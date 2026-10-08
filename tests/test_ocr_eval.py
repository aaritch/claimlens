import shutil

import pytest

from claimlens.cli import main
from claimlens.eval.report import render_compare, render_report
from claimlens.eval.runner import RunConfig, build_ocr_for, evaluate, parse_config
from claimlens.config import get_settings
from claimlens.llm.mock import MockLLM

HAS_TESSERACT = shutil.which("tesseract") is not None


def run(data, ocr="none", **kw):
    cfg = RunConfig(label=f"ocr-{ocr}", llm="mock:careful", ocr=ocr)
    llm = MockLLM("careful")
    return evaluate(data, cfg, llm, llm, ocr=build_ocr_for(cfg, get_settings(), data), **kw)


def test_ocr_key_parses_in_config():
    assert parse_config("label=x,llm=mock:careful,ocr=tesseract+textract").ocr == "tesseract+textract"


def test_without_ocr_scans_pile_up_at_needs_ocr(scan_samples):
    r = run(scan_samples, "none")
    assert r.n_scanned > 0 and r.n_needs_ocr >= r.n_scanned and r.exact_match_rate_scanned == 0.0
    assert r.n_ocr_docs == 0 and r.ocr_cost_usd == 0


def test_with_oracle_ocr_scans_are_read_and_costed(scan_samples):
    r = run(scan_samples, "oracle")
    assert r.exact_match_rate_scanned == 1.0 and r.exact_match_rate_native == 1.0
    assert r.n_ocr_docs >= r.n_scanned - 0 and r.ocr_pages >= r.n_ocr_docs and r.ocr_cost_usd > 0
    assert r.total_cost_usd >= r.ocr_cost_usd


def test_noisy_ocr_hurts_extraction_but_not_auto_accept_safety(scan_samples):
    noisy = run(scan_samples, "oracle:0.25")
    assert noisy.exact_match_rate_scanned < 1.0
    assert noisy.auto_accept_precision == 1.0 and noisy.error_catch_rate == 1.0  # NPI check digit + confidence gate
    assert any(e.field == "provider_npi" for e in noisy.errors)


def test_ocr_errors_are_attributed_to_ocr_quality(scan_samples):
    from claimlens.eval.report import likely_cause
    errs = run(scan_samples, "oracle:0.25").errors
    assert errs and all(likely_cause(e) == "ocr_quality" for e in errs)


def test_report_has_ocr_section_and_compare_shows_deltas(scan_samples):
    none, oracle = run(scan_samples, "none"), run(scan_samples, "oracle")
    assert "## OCR" in render_report(oracle)
    cmp = render_compare(none, oracle)
    assert "Exact-match: scanned docs" in cmp and "OCR cost" in cmp and "(better)" in cmp


def test_cascade_escalation_is_counted_and_paid_for(scan_samples, monkeypatch):
    # With an impossible confidence bar everything escalates; oracle+oracle is the offline stand-in.
    monkeypatch.setenv("CLAIMLENS_MIN_OCR_CONFIDENCE", "99.5")
    from claimlens.config import get_settings as gs
    cfg = RunConfig(label="cascade", llm="mock:careful", ocr="oracle:0.1+oracle")
    llm = MockLLM("careful")
    r = evaluate(scan_samples, cfg, llm, llm, settings=gs(), ocr=build_ocr_for(cfg, gs(), scan_samples))
    assert r.ocr_escalations > 0 and r.exact_match_rate_scanned == 1.0  # the clean second read rescued every scan


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract binary not installed")
def test_real_tesseract_beats_no_ocr(scan_samples):
    none, tess = run(scan_samples, "none"), run(scan_samples, "tesseract", workers=2)
    assert tess.exact_match_rate_scanned > none.exact_match_rate_scanned
    # Regression guard from a real finding: Tesseract garbled a provider name and dropped a service date;
    # the name-plausibility and OCR-completeness rules now route those to a human. Tolerance, not exactness,
    # because OCR output can differ slightly across Tesseract versions.
    assert tess.error_catch_rate is None or tess.error_catch_rate >= 0.9
    assert tess.ocr_cost_usd == 0 and tess.ocr_pages > 0


def test_cli_scan_flow(tmp_path, capsys):
    data = tmp_path / "d"
    assert main(["gen-data", "--out", str(data), "--count", "9", "--scan-fraction", "0.5"]) == 0
    assert main(["run-dir", str(data), "--mock", "--ocr", "none", "--out", str(tmp_path / "r0")]) == 0
    assert "needs_ocr=" in capsys.readouterr().out
    assert main(["run-dir", str(data), "--mock", "--ocr", "oracle", "--out", str(tmp_path / "r1")]) == 0
    out = capsys.readouterr().out
    assert "[OCR oracle" in out


def test_cli_unknown_ocr_engine_exits_cleanly(tmp_path, capsys):
    main(["gen-data", "--out", str(tmp_path), "--count", "3"])
    assert main(["run-dir", str(tmp_path), "--mock", "--ocr", "magic"]) == 2
    assert "OCR configuration error" in capsys.readouterr().err
