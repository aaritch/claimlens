import pytest

from claimlens.datagen import generate_samples


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("samples")
    generate_samples(out, count=12, seed=7)
    return out


@pytest.fixture(scope="session")
def hard_samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("hard")
    generate_samples(out, count=60, seed=3, difficulty="hard")
    return out


@pytest.fixture(scope="session")
def scan_samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("scans")
    generate_samples(out, count=24, seed=11, difficulty="clean", scan_fraction=0.5)
    return out


# ---------------------------------------------------------------------------------------------
# Phase 4 fixtures: databases, idempotency stores, helpers
# ---------------------------------------------------------------------------------------------
import os

from claimlens.models import (Classification, DocType, ExtractedClaim, Finding, ProcessingResult, Usage)
from claimlens.storage.db import Base, init_db, make_engine, make_session_factory

PG_URL = os.getenv("CLAIMLENS_TEST_PG_URL", "")


@pytest.fixture(params=["sqlite"] + (["postgres"] if PG_URL else []))
def session_factory(request, tmp_path):
    url = f"sqlite:///{tmp_path / 'test.db'}" if request.param == "sqlite" else PG_URL
    engine = make_engine(url)
    Base.metadata.drop_all(engine)
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


def make_result(status="needs_review", doc_type=DocType.PROVIDER_APPEAL, **claim_overrides) -> ProcessingResult:
    claim = dict(provider_npi="1234567893", provider_name="Riverbend Orthopedic Group", payer_name="Aetna",
                 claim_reference="CLM-123456", service_date="2024-03-04", disputed_amount=1450.5,
                 billing_violation_type=None, summary="s")
    claim.update(claim_overrides)
    extracted = ExtractedClaim(**claim)
    return ProcessingResult(
        filename="upload.pdf", status=status, file_type="pdf", page_count=1,
        classification=Classification(doc_type=doc_type, confidence=0.95, rationale="x"),
        claim=extracted, raw_claim=extracted,
        findings=[Finding(rule="required_field", severity="soft", message="Missing required field: x")] if status == "needs_review" else [],
        usage=Usage(llm_calls=2, input_tokens=900, output_tokens=150, cost_usd=0.004),
        model_id="mock", prompt_versions={"classify": "classify_v1", "extract": "extract_v1"}, elapsed_ms=12)
