import io
import json
import shutil

import pytest
from botocore.exceptions import ClientError

from claimlens.datagen import generate_samples, render_scan_pages, scan_pdf_bytes, scan_png_bytes, write_pdf
from claimlens.llm.mock import MockLLM
from claimlens.llm.pricing import estimate_ocr_cost
from claimlens.ocr.base import OcrError, OcrResult, apply_ocr
from claimlens.ocr.cascade import CascadeOcr
from claimlens.ocr.factory import build_ocr
from claimlens.ocr.oracle import OracleOcr
from claimlens.ocr.textract import TextractEngine, blocks_to_text
from claimlens.config import get_settings
from claimlens.parsers.router import parse_bytes, parse_file
from claimlens.pipeline import process_file
import random

HAS_TESSERACT = shutil.which("tesseract") is not None


def line(text, page=1, conf=99.0):
    return {"BlockType": "LINE", "Text": text, "Page": page, "Confidence": conf}


# ---- fakes ---------------------------------------------------------------------

class FakeTextract:
    def __init__(self, sync_blocks=None, statuses=None, error=None):
        self.sync_blocks = sync_blocks or [line("hello world")]
        self.statuses = list(statuses or ["SUCCEEDED"])
        self.error, self.sync_calls, self.started, self.polls = error, [], [], 0

    def detect_document_text(self, Document):
        if self.error:
            raise self.error
        self.sync_calls.append(Document)
        return {"Blocks": self.sync_blocks}

    def start_document_text_detection(self, DocumentLocation):
        self.started.append(DocumentLocation)
        return {"JobId": "job-1"}

    def get_document_text_detection(self, JobId, NextToken=None):
        if NextToken == "t1":
            return {"JobStatus": "SUCCEEDED", "Blocks": [line("page two text", page=2, conf=90.0)]}
        self.polls += 1
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status != "SUCCEEDED":
            return {"JobStatus": status}
        return {"JobStatus": "SUCCEEDED", "DocumentMetadata": {"Pages": 2}, "NextToken": "t1",
                "Blocks": [line("page one text", page=1, conf=100.0)]}


class FakeS3:
    def __init__(self):
        self.uploaded, self.deleted = [], []

    def upload_fileobj(self, fileobj, bucket, key):
        self.uploaded.append((bucket, key, len(fileobj.read())))

    def delete_object(self, Bucket, Key):
        self.deleted.append((Bucket, Key))


def multipage_pdf():
    buf = io.BytesIO()
    write_pdf([f"line {i}" for i in range(130)], buf)  # > 1 page
    return buf.getvalue()


def engine(client, s3=None, bucket="", **kw):
    return TextractEngine(textract_client=client, s3_client=s3, bucket=bucket, sleep=lambda s: None, **kw)


# ---- Textract ---------------------------------------------------------------------

def test_blocks_to_text_orders_pages_and_averages_confidence():
    blocks = [line("B2", page=2, conf=80), line("A1", page=1, conf=100), {"BlockType": "WORD", "Text": "ignored"}]
    text, conf, pages = blocks_to_text(blocks)
    assert text == "A1\n\nB2" and conf == 90 and pages == 2


def test_textract_image_uses_sync_with_bytes():
    fake = FakeTextract()
    result = engine(fake).extract(b"\x89PNG-bytes", "scan.png")
    assert fake.sync_calls == [{"Bytes": b"\x89PNG-bytes"}]
    assert result.text == "hello world" and result.cost_usd == estimate_ocr_cost("textract", 1)


def test_textract_multipage_without_bucket_splits_into_single_page_sync_calls():
    fake = FakeTextract()
    result = engine(fake).extract(multipage_pdf(), "big.pdf")
    assert len(fake.sync_calls) == result.pages >= 2
    assert result.cost_usd == estimate_ocr_cost("textract", result.pages)


def test_textract_multipage_with_bucket_uses_async_polls_paginates_and_cleans_up():
    fake, s3 = FakeTextract(statuses=["IN_PROGRESS", "IN_PROGRESS", "SUCCEEDED"]), FakeS3()
    result = engine(fake, s3, bucket="b").extract(multipage_pdf(), "big.pdf")
    assert fake.sync_calls == [] and len(fake.started) == 1 and fake.polls == 3
    assert "page one text" in result.text and "page two text" in result.text  # pagination followed
    assert result.pages == 2 and result.mean_confidence == 95.0
    assert s3.uploaded[0][0] == "b" and s3.deleted == [("b", s3.uploaded[0][1])]


@pytest.mark.parametrize("statuses,msg", [(["FAILED"], "failed"), (["IN_PROGRESS"], "timed out")])
def test_textract_async_failure_and_timeout_still_delete_the_s3_object(statuses, msg):
    s3 = FakeS3()
    eng = engine(FakeTextract(statuses=statuses), s3, bucket="b", poll_seconds=2, timeout_seconds=5)
    with pytest.raises(OcrError, match=msg):
        eng.extract(multipage_pdf(), "big.pdf")
    assert len(s3.deleted) == 1  # patient documents are never left behind in the temp bucket


def test_textract_client_error_becomes_ocrerror_with_code_only():
    err = ClientError({"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "secret detail"}}, "x")
    with pytest.raises(OcrError) as info:
        engine(FakeTextract(error=err)).extract(b"x", "a.png")
    assert "ProvisionedThroughputExceededException" in str(info.value) and "secret" not in str(info.value)


def test_textract_rejects_unsupported_extension():
    with pytest.raises(OcrError, match="cannot read"):
        engine(FakeTextract()).extract(b"x", "a.docx")


# ---- Tesseract (real OCR!) ---------------------------------------------------------------

@pytest.fixture(scope="module")
def eng():
    from claimlens.ocr.tesseract import TesseractEngine
    return TesseractEngine()


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract binary not installed")
class TestRealTesseract:
    LINES = ["Provider Appeal of payment determination CLM-482910", "Billing provider: Riverbend Orthopedic Group (NPI: 1234567893)",
             "Payer: Aetna", "Underpaid amount in dispute: $4,210.50"]

    def test_reads_a_scanned_pdf(self, eng):
        images, _ = render_scan_pages(self.LINES, random.Random(1))
        result = eng.extract(scan_pdf_bytes(images), "x.pdf")
        assert "CLM-482910" in result.text and "Aetna" in result.text
        assert result.pages == 1 and 50 < result.mean_confidence <= 100 and result.cost_usd == 0

    def test_reads_a_png(self, eng):
        images, _ = render_scan_pages(self.LINES, random.Random(2))
        assert "Riverbend" in eng.extract(scan_png_bytes(images[0]), "x.png").text

    def test_multipage_pdf_counts_pages(self, eng):
        images, _ = render_scan_pages(self.LINES * 3, random.Random(3), lines_per_page=4)
        result = eng.extract(scan_pdf_bytes(images), "x.pdf")
        assert result.pages == len(images) >= 3

    def test_garbage_input_is_ocrerror(self, eng):
        with pytest.raises(OcrError):
            eng.extract(b"not an image", "x.png")


# ---- cascade --------------------------------------------------------------------------------

class Stub:
    def __init__(self, name, text="x" * 500, conf=95.0, cost=0.0, fail=False):
        self.name, self.text, self.conf, self.cost, self.fail, self.calls = name, text, conf, cost, fail, 0

    def extract(self, data, filename):
        self.calls += 1
        if self.fail:
            raise OcrError("boom")
        return OcrResult(self.text, 1, self.name, self.conf, self.cost)


def test_cascade_keeps_cheap_result_when_confident():
    cheap, pricey = Stub("cheap", conf=95), Stub("pricey", cost=0.01)
    r = CascadeOcr(cheap, pricey, min_confidence=80).extract(b"", "a.pdf")
    assert r.engine == "cheap" and pricey.calls == 0 and not r.escalated


def test_cascade_escalates_on_low_confidence_and_sums_cost():
    cheap, pricey = Stub("cheap", conf=60, cost=0.001), Stub("pricey", text="y" * 500, conf=98, cost=0.01)
    r = CascadeOcr(cheap, pricey, min_confidence=80).extract(b"", "a.pdf")
    assert r.escalated and r.text.startswith("y") and r.engines_used == ["cheap", "pricey"]
    assert r.cost_usd == pytest.approx(0.011)


def test_cascade_escalates_on_sparse_text():
    r = CascadeOcr(Stub("cheap", text="tiny"), Stub("pricey"), min_chars_per_page=100).extract(b"", "a.pdf")
    assert r.escalated


def test_cascade_falls_back_to_cheap_result_if_expensive_engine_fails():
    r = CascadeOcr(Stub("cheap", conf=60), Stub("pricey", fail=True), min_confidence=80).extract(b"", "a.pdf")
    assert r.engine == "cheap" and not r.escalated and r.mean_confidence == 60


# ---- oracle, factory, apply_ocr -------------------------------------------------------------------

def test_oracle_lookup_and_missing(tmp_path, scan_samples):
    scan = next(scan_samples.glob("*_scan.pdf"))
    oracle = OracleOcr(scan_samples / "ocr_oracle")
    assert "NPI" in oracle.extract(scan.read_bytes(), scan.name).text
    with pytest.raises(OcrError, match="no oracle text"):
        oracle.extract(b"unknown bytes", "x.pdf")


def test_oracle_error_rate_garbles_digits_deterministically(scan_samples):
    scan = next(scan_samples.glob("*_scan.pdf"))
    clean = OracleOcr(scan_samples / "ocr_oracle").extract(scan.read_bytes(), scan.name).text
    noisy_a = OracleOcr(scan_samples / "ocr_oracle", error_rate=0.3).extract(scan.read_bytes(), scan.name)
    noisy_b = OracleOcr(scan_samples / "ocr_oracle", error_rate=0.3).extract(scan.read_bytes(), scan.name)
    assert noisy_a.text == noisy_b.text != clean and noisy_a.mean_confidence < 98


def test_factory_specs(scan_samples):
    s = get_settings()
    assert build_ocr("none", s) is None
    assert build_ocr("oracle:0.02", s, oracle_dir=scan_samples / "ocr_oracle").error_rate == 0.02
    assert build_ocr("oracle+oracle", s, oracle_dir=scan_samples / "ocr_oracle").name == "oracle+oracle"
    with pytest.raises(OcrError, match="Unknown OCR engine"):
        build_ocr("magic", s)
    with pytest.raises(OcrError, match="ocr_oracle"):
        build_ocr("oracle", s)


def test_apply_ocr_recurses_into_email_attachments(scan_samples):
    eml = next(p for p in scan_samples.glob("*.eml") if parse_file(p).any_needs_ocr)
    parsed = parse_file(eml)
    assert parsed.attachments[0].needs_ocr and parsed.attachments[0].raw_bytes
    summary = apply_ocr(parsed, OracleOcr(scan_samples / "ocr_oracle"))
    assert summary.documents == 1 and not parsed.any_needs_ocr
    assert "NPI" in parsed.full_text() and parsed.attachments[0].raw_bytes is None


def test_apply_ocr_empty_result_is_an_error():
    parsed = parse_bytes(b"\x89PNG", "x.png")
    with pytest.raises(OcrError, match="no text"):
        apply_ocr(parsed, Stub("empty", text="  "))


# ---- pipeline integration ----------------------------------------------------------------------------

def scanned_files(data):
    out = []
    for f in sorted((data / "ground_truth").glob("*.json")):
        t = json.loads(f.read_text())
        if "scanned" in t["injected"]:
            out.append((data / t["filename"], t))
    return out


def test_scans_stop_at_needs_ocr_without_engine_and_succeed_with_one(scan_samples):
    llm, oracle = MockLLM(), OracleOcr(scan_samples / "ocr_oracle")
    files = scanned_files(scan_samples)
    assert files
    for path, truth in files:
        assert process_file(path, llm).status == "needs_ocr"
        r = process_file(path, llm, ocr=oracle)
        assert r.status == "validated", (path.name, r.findings, r.error)
        assert r.ocr.pages == max(truth["scan_pages"], 1) and r.usage.ocr_pages == r.ocr.pages
        assert r.claim.provider_npi == truth["expected"]["provider_npi"]


def test_total_cost_is_llm_plus_ocr(scan_samples):
    class PricedLLM(MockLLM):
        def call_tool(self, **kw):
            res = super().call_tool(**kw)
            res.cost_usd = 0.01
            return res

    path, _ = scanned_files(scan_samples)[0]
    r = process_file(path, PricedLLM(), ocr=OracleOcr(scan_samples / "ocr_oracle"))
    assert r.usage.ocr_cost_usd == estimate_ocr_cost("oracle", r.usage.ocr_pages) > 0
    assert r.usage.cost_usd == pytest.approx(0.02 + r.usage.ocr_cost_usd)


def test_low_ocr_confidence_can_never_be_auto_accepted(scan_samples):
    path, _ = scanned_files(scan_samples)[0]
    shaky = OracleOcr(scan_samples / "ocr_oracle", error_rate=0.0)
    original = shaky.extract
    shaky.extract = lambda d, n: (lambda r: (setattr(r, "mean_confidence", 40.0), r)[1])(original(d, n))
    r = process_file(path, MockLLM(), ocr=shaky)
    assert r.status == "needs_review" and any(f.rule == "low_ocr_confidence" for f in r.findings)


def test_ocr_failure_becomes_an_error_status(scan_samples):
    path, _ = scanned_files(scan_samples)[0]
    r = process_file(path, MockLLM(), ocr=Stub("broken", fail=True))
    assert r.status == "error" and r.error.startswith("OCR failed")


@pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract binary not installed")
def test_end_to_end_with_real_tesseract_never_leaks_bad_data(scan_samples):
    """Whatever Tesseract misreads, a wrong document must not be auto-accepted by the NPI-check/rules combo
    for the NPI field. (Other fields can slip through, which is exactly why evaluation exists.)"""
    from claimlens.ocr.tesseract import TesseractEngine
    eng = TesseractEngine()
    for path, truth in scanned_files(scan_samples)[:3]:
        r = process_file(path, MockLLM(), ocr=eng)
        assert r.ocr and r.ocr.engines == ["tesseract"]
        if r.status == "validated":
            assert r.claim.provider_npi == truth["expected"]["provider_npi"]


# ---- data generation ----------------------------------------------------------------------------------

def test_scan_generation_flags_truth_and_writes_oracle(scan_samples):
    scanned = scanned_files(scan_samples)
    assert 4 <= len(scanned) <= 20
    assert len(list((scan_samples / "ocr_oracle").glob("*.txt"))) == len(scanned)
    assert any(t["scan_pages"] >= 2 for _, t in scanned)  # multi-page scans exist


def test_scan_pdfs_have_no_text_layer(scan_samples):
    path, _ = next((p, t) for p, t in scanned_files(scan_samples) if p.suffix == ".pdf")
    parsed = parse_file(path)
    assert parsed.needs_ocr and parsed.raw_bytes


def test_enabling_scans_does_not_change_the_unscanned_documents(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate_samples(a, 12, seed=4)
    generate_samples(b, 12, seed=4, scan_fraction=0.5)
    same = [t for t in (b / "ground_truth").glob("*.json") if "scanned" not in json.loads(t.read_text())["injected"]]
    assert same
    for t in same:
        assert json.loads(t.read_text())["expected"] == json.loads((a / "ground_truth" / t.name).read_text())["expected"]


def test_scan_fraction_validation(tmp_path):
    with pytest.raises(ValueError):
        generate_samples(tmp_path, 3, scan_fraction=1.5)
