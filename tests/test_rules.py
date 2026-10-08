from datetime import date

import pytest

from claimlens.config import get_settings
from claimlens.models import DocType, ExtractedClaim
from claimlens.rules.engine import validate_claim
from claimlens.rules.payers import normalize_payer

TODAY = date(2026, 1, 1)


def good_claim(**overrides):
    base = dict(provider_npi="1234567893", provider_name="Acme Clinic", payer_name="Aetna",
                claim_reference="CLM-123456", service_date="2025-06-01", disputed_amount=1200.0,
                billing_violation_type="surprise_billing")
    base.update(overrides)
    return ExtractedClaim(**base)


def run(claim, doc_type=DocType.SURPRISE_BILLING_COMPLAINT):
    return validate_claim(doc_type, claim, get_settings(), today=TODAY)


def test_clean_claim_validates():
    _, findings, status = run(good_claim())
    assert status == "validated" and not [f for f in findings if f.severity != "info"]


def test_missing_required_field_needs_review():
    _, findings, status = run(good_claim(disputed_amount=None))
    assert status == "needs_review"
    assert any(f.rule == "required_field" for f in findings)


def test_bad_npi_is_rejected():
    _, _, status = run(good_claim(provider_npi="1234567894"))
    assert status == "rejected"


def test_future_date_is_rejected():
    _, _, status = run(good_claim(service_date="2027-01-01"))
    assert status == "rejected"


def test_non_iso_date_is_rejected():
    _, _, status = run(good_claim(service_date="03/04/2025"))
    assert status == "rejected"


@pytest.mark.parametrize("amount", [0, -5])
def test_non_positive_amount_rejected(amount):
    _, _, status = run(good_claim(disputed_amount=amount))
    assert status == "rejected"


def test_huge_amount_goes_to_review():
    _, _, status = run(good_claim(disputed_amount=9_999_999))
    assert status == "needs_review"


def test_unknown_payer_needs_review():
    _, _, status = run(good_claim(payer_name="Totally Made Up Health"))
    assert status == "needs_review"


def test_payer_is_normalized():
    claim, _, status = run(good_claim(payer_name="Blue Cross Blue Shield"))
    assert claim.payer_name == "BlueCross BlueShield" and status == "validated"


def test_npi_with_dashes_is_cleaned():
    claim, _, status = run(good_claim(provider_npi="123-456-7893"))
    assert claim.provider_npi == "1234567893" and status == "validated"


def test_normalize_payer_fuzzy_and_unknown():
    assert normalize_payer("UnitedHealth care") == "UnitedHealthcare"
    assert normalize_payer("Acme Mutual") is None


# ---- Phase 3 rules ------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Harborview \u2014_\u2014\u2014s Partners", "Riverbend | Group", "Acme\ufffd Clinic"])
def test_garbled_provider_name_needs_review(name):
    _, findings, status = run(good_claim(provider_name=name))
    assert status == "needs_review" and any(f.rule == "name_plausible" for f in findings)


@pytest.mark.parametrize("name", ["Riverbend Orthopedic Group", "St. Mary's Hospital, Inc.", "Cl\u00ednica Santa Mar\u00eda", "A&B Imaging (East)"])
def test_normal_provider_names_pass(name):
    _, _, status = run(good_claim(provider_name=name))
    assert status == "validated"


def test_missing_optional_field_is_fine_for_native_text_but_flagged_after_ocr():
    appeal = good_claim(service_date=None)
    _, _, native = validate_claim(DocType.PROVIDER_APPEAL, appeal, get_settings(), today=TODAY)
    _, findings, ocr = validate_claim(DocType.PROVIDER_APPEAL, appeal, get_settings(), today=TODAY, ocr_used=True)
    assert native == "validated" and ocr == "needs_review"
    assert [f.rule for f in findings if f.severity == "soft"] == ["ocr_completeness"]


def test_ocr_completeness_does_not_double_report_required_fields():
    _, findings, _ = validate_claim(DocType.PROVIDER_APPEAL, good_claim(claim_reference=None), get_settings(),
                                    today=TODAY, ocr_used=True)
    assert [f.rule for f in findings if f.severity == "soft"] == ["required_field"]
