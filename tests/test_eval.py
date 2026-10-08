import json

import pytest

from claimlens.cli import main
from claimlens.datagen import generate_samples
from claimlens.eval.report import likely_cause, render_compare, render_report
from claimlens.eval.runner import RunConfig, evaluate, parse_config
from claimlens.llm.bedrock_client import ToolCallResult
from claimlens.llm.mock import MockLLM


def run(data, profile="careful", **kw):
    llm = MockLLM(profile)
    return evaluate(data, RunConfig(label=profile, llm=f"mock:{profile}"), llm, llm, **kw)


# ---- config parsing -----------------------------------------------------------

def test_parse_config_with_colons_in_model_ids():
    cfg = parse_config("label=v2,llm=bedrock,extract_prompt=extract_v2,model_id=vendor.model-v1:0")
    assert cfg.label == "v2" and cfg.extract_prompt == "extract_v2" and cfg.model_id == "vendor.model-v1:0"


def test_parse_config_rejects_typos():
    with pytest.raises(ValueError, match="Unknown config key"):
        parse_config("label=x,extract_promt=extract_v2")
    with pytest.raises(ValueError, match="key=value"):
        parse_config("label")


# ---- scoring on clean data ------------------------------------------------------

def test_clean_data_scores_perfect(samples):
    r = run(samples)
    assert r.n_docs == 12
    assert r.micro.f1 == 1.0 and r.exact_match_rate == 1.0 and r.classification_accuracy == 1.0
    assert r.auto_accept_precision == 1.0 and r.error_catch_rate is None and not r.errors


def test_regression_gate_like_ci(samples):
    """This is what a CI step would assert: quality must not drop below a floor."""
    assert run(samples).micro.f1 >= 0.95


# ---- hard data: the interesting behaviour ---------------------------------------

def test_hard_data_careful_vs_naive(hard_samples):
    naive, careful = run(hard_samples, "naive"), run(hard_samples, "careful")
    assert careful.micro.f1 > naive.micro.f1
    assert careful.exact_match_rate > naive.exact_match_rate
    # The rules engine catches every OCR-style NPI typo, so nothing wrong slips through:
    assert careful.auto_accept_precision == 1.0 and careful.error_catch_rate == 1.0
    # ...but plausible-looking wrong amounts are invisible to the rules, so naive leaks bad data:
    assert naive.auto_accept_precision < 1.0 and naive.error_catch_rate < 1.0
    assert any(e.auto_accepted and e.field == "disputed_amount" for e in naive.errors)


def test_every_careful_error_is_explained_by_an_injected_difficulty(hard_samples):
    for e in run(hard_samples, "careful").errors:
        assert likely_cause(e) != "unexplained"


def test_rules_engine_effect_is_measured(hard_samples):
    r = run(hard_samples, "careful")
    assert r.raw_micro is not None and r.micro.f1 >= r.raw_micro.f1


def test_worker_count_does_not_change_results(hard_samples):
    a, b = run(hard_samples, workers=1), run(hard_samples, workers=8)
    assert a.micro == b.micro and [d.status for d in a.docs] == [d.status for d in b.docs]


# ---- privacy of reports ------------------------------------------------------------

def test_values_hidden_by_default_and_shown_on_request(hard_samples):
    hidden = run(hard_samples, "naive")
    shown = run(hard_samples, "naive", show_values=True)
    assert all(e.expected is None and e.got is None for e in hidden.errors)
    assert any(e.expected is not None for e in shown.errors)
    assert "hidden by default" in render_report(hidden)


# ---- a misbehaving model ---------------------------------------------------------------

class AlwaysOtherLLM:
    model_id = "always-other"

    def call_tool(self, *, tool_name, **kw):
        return ToolCallResult({"doc_type": "other", "confidence": 0.99, "rationale": "?"}, 100, 10, 0.002, 1, "x")


def test_misclassifying_model_scores_zero_recall(samples):
    llm = AlwaysOtherLLM()
    r = evaluate(samples, RunConfig(label="bad", llm="mock"), llm, llm)
    assert r.classification_accuracy == 0.0 and r.micro.recall == 0.0
    assert r.status_counts == {"needs_review": 12}
    assert r.error_catch_rate == 1.0           # nothing was auto-accepted, so nothing wrong slipped through
    assert r.total_cost_usd == pytest.approx(12 * 0.002)
    assert r.cost_per_exact_doc is None         # no perfect docs -> undefined, not a crash


def test_missing_ground_truth_is_a_clear_error(tmp_path):
    with pytest.raises(ValueError, match="gen-data"):
        run(tmp_path)


def test_clean_and_hard_generation_are_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate_samples(a, 6, seed=5, difficulty="hard")
    generate_samples(b, 6, seed=5, difficulty="hard")
    assert (a / "ground_truth" / "01_surprise_billing_complaint.json").read_text() == \
           (b / "ground_truth" / "01_surprise_billing_complaint.json").read_text()


def test_hard_mode_really_breaks_npi_check_digit(hard_samples):
    from claimlens.rules.npi import is_valid_npi
    from claimlens.parsers.router import parse_file
    import re
    corrupted = [json.loads(p.read_text()) for p in (hard_samples / "ground_truth").glob("*.json")
                 if "npi_ocr_error" in json.loads(p.read_text())["injected"]]
    assert corrupted
    for t in corrupted:
        text = parse_file(hard_samples / t["filename"]).full_text()
        written = re.search(r"(?:NPI|National Provider Identifier)\D{0,40}(\d{10})", text).group(1)
        assert written != t["expected"]["provider_npi"] and not is_valid_npi(written)


# ---- rendering + CLI ------------------------------------------------------------------------

def test_reports_render(hard_samples):
    a, b = run(hard_samples, "naive"), run(hard_samples, "careful")
    single, compare = render_report(a), render_compare(a, b)
    assert "Auto-accept precision" in single and "False accepts" in single
    assert "B fixed" in compare and "Verdict" in compare and "(better)" in compare


def test_cli_eval_gate_exit_codes(hard_samples, tmp_path, capsys):
    base = ["eval", "--data", str(hard_samples), "--out", str(tmp_path)]
    assert main(base + ["--config", "label=c,llm=mock:careful", "--min-auto-accept-precision", "0.99"]) == 0
    assert main(base + ["--config", "label=n,llm=mock:naive", "--min-auto-accept-precision", "0.99"]) == 1
    assert "GATE FAILED" in capsys.readouterr().err
    assert (tmp_path / "n.eval.json").exists() and (tmp_path / "n.eval.md").exists()


def test_cli_compare_writes_files(hard_samples, tmp_path):
    code = main(["compare", "--data", str(hard_samples), "--out", str(tmp_path),
                 "--config", "label=A,llm=mock:naive", "--config", "label=B,llm=mock:careful"])
    assert code == 0 and (tmp_path / "compare_A_vs_B.md").exists()


def test_cli_hard_gen_flag(tmp_path):
    assert main(["gen-data", "--out", str(tmp_path), "--count", "6", "--difficulty", "hard"]) == 0
    assert json.loads(next((tmp_path / "ground_truth").glob("*.json")).read_text())["difficulty"] == "hard"
