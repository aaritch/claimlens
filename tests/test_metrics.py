import pytest

from claimlens.eval.metrics import (CORRECT, BOTH_NULL, HALLUCINATED, MISSED, WRONG, Counts,
                                    classify_outcome, normalize, percentile, score_document)


@pytest.mark.parametrize("field,exp,pred,outcome", [
    ("payer_name", "Aetna", " aetna ", CORRECT),           # case/space-insensitive
    ("disputed_amount", 1450.5, 1450.50, CORRECT),
    ("disputed_amount", 1450.5, 1450.51, WRONG),
    ("provider_npi", "1234567893", "123-456-7893", CORRECT),  # digits only
    ("service_date", None, None, BOTH_NULL),
    ("service_date", None, "2024-01-01", HALLUCINATED),
    ("service_date", "2024-01-01", None, MISSED),
    ("service_date", "2024-01-01", "2024-01-02", WRONG),
    ("claim_reference", "CLM-1", "", MISSED),               # empty string counts as null
])
def test_outcomes(field, exp, pred, outcome):
    assert classify_outcome(field, exp, pred) == outcome


def test_wrong_value_counts_as_both_fp_and_fn():
    c = Counts()
    c.add(WRONG)
    assert (c.tp, c.fp, c.fn) == (0, 1, 1)


def test_precision_recall_f1_hand_computed():
    c = Counts()
    for o in [CORRECT] * 6 + [WRONG] * 2 + [MISSED] * 1 + [HALLUCINATED] * 1 + [BOTH_NULL] * 5:
        c.add(o)
    # tp=6, fp=2(wrong)+1(halluc)=3, fn=2(wrong)+1(missed)=3
    assert (c.tp, c.fp, c.fn, c.tn) == (6, 3, 3, 5)
    assert c.precision == pytest.approx(6 / 9)
    assert c.recall == pytest.approx(6 / 9)
    assert c.f1 == pytest.approx(12 / 18)


def test_undefined_metrics_are_none_not_crashes():
    c = Counts()
    c.add(BOTH_NULL)
    assert c.precision is None and c.recall is None and c.f1 is None


def test_no_prediction_means_everything_missed():
    out = score_document({"provider_npi": "1234567893", "service_date": None}, None)
    assert out["provider_npi"] == MISSED and out["service_date"] == BOTH_NULL


def test_normalize_handles_junk_amount():
    assert normalize("disputed_amount", "not a number") == "not a number"


def test_percentile_nearest_rank():
    assert percentile([10, 20, 30, 40, 50], 50) == 30
    assert percentile([10, 20, 30, 40, 50], 95) == 50
    assert percentile([], 95) == 0.0
