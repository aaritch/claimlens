import json

import pytest

from claimlens.datagen import write_docx
from claimlens.llm.bedrock_client import ToolCallResult
from claimlens.llm.mock import MockLLM
from claimlens.pipeline import process_file


def test_mini_eval_against_answer_keys(samples):
    """A tiny preview of Phase 2: compare pipeline output to ground truth, field by field."""
    llm = MockLLM()
    checked = 0
    for key_file in sorted((samples / "ground_truth").glob("*.json")):
        truth = json.loads(key_file.read_text())
        result = process_file(samples / truth["filename"], llm)
        assert result.status == "validated", (truth["filename"], result.findings, result.error)
        assert result.classification.doc_type.value == truth["doc_type"]
        got = result.claim.model_dump()
        for field, expected in truth["expected"].items():
            if truth["doc_type"] != "surprise_billing_complaint" and field == "billing_violation_type":
                continue
            assert got[field] == expected, (truth["filename"], field)
        checked += 1
    assert checked == 12


def test_scanned_pdf_short_circuits_before_any_llm_call(samples):
    result = process_file(samples / "scan_stub_01.pdf", MockLLM())
    assert result.status == "needs_ocr" and result.usage.llm_calls == 0


def test_unsupported_file_is_an_error_not_a_crash(tmp_path):
    bad = tmp_path / "x.xyz"
    bad.write_bytes(b"hi")
    assert process_file(bad, MockLLM()).status == "error"


class ScriptedLLM:
    """Returns whatever we tell it: lets us simulate a misbehaving model."""
    model_id = "scripted"

    def __init__(self, classification, extraction):
        self.classification, self.extraction, self.calls = classification, extraction, 0

    def call_tool(self, *, tool_name, **kw):
        self.calls += 1
        data = self.classification if tool_name == "record_classification" else self.extraction
        return ToolCallResult(data, 100, 50, 0.001, 5, "scripted")


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "d.docx"
    write_docx(["Provider Appeal", "some text " * 20], p)
    return p


GOOD_CLS = {"doc_type": "provider_appeal", "confidence": 0.9, "rationale": "x"}


def test_model_hallucinated_npi_is_rejected_by_rules(doc):
    llm = ScriptedLLM(GOOD_CLS, {"provider_npi": "1234567894", "payer_name": "Aetna",
                                 "disputed_amount": 100.0, "claim_reference": "CLM-111111"})
    result = process_file(doc, llm)
    assert result.status == "rejected"
    assert result.usage.llm_calls == 2 and result.usage.cost_usd == 0.002


def test_schema_violation_becomes_error_without_leaking_values(doc):
    llm = ScriptedLLM(GOOD_CLS, {"disputed_amount": "SECRET-VALUE-not-a-number"})
    result = process_file(doc, llm)
    assert result.status == "error"
    assert "disputed_amount" in result.error and "SECRET-VALUE" not in result.error


def test_low_confidence_skips_extraction(doc):
    llm = ScriptedLLM({"doc_type": "provider_appeal", "confidence": 0.3, "rationale": "x"}, {})
    result = process_file(doc, llm)
    assert result.status == "needs_review" and llm.calls == 1


def test_other_doc_type_skips_extraction(doc):
    llm = ScriptedLLM({"doc_type": "other", "confidence": 0.99, "rationale": "x"}, {})
    assert process_file(doc, llm).status == "needs_review" and llm.calls == 1
