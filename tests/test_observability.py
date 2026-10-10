import dataclasses
import json

import pytest

from claimlens.config import get_settings
from claimlens.observability import METRICS, Metrics
from claimlens.service import ProcessingService, TransientProcessingError
from claimlens.storage.idempotency import SqlIdempotencyStore
from tests.conftest import make_result
from tests.test_service import CountingLLM


def test_emf_contains_only_bounded_metadata_and_aggregate_dimensions():
    lines = []
    result = make_result("validated")
    result.filename = "Jane_Example_123-45-6789.pdf"
    result.claim.provider_name = "Sensitive Name"
    Metrics("test", True, lines.append).record(result)
    payload = json.loads(lines[0])
    metadata = payload["_aws"]["CloudWatchMetrics"][0]
    assert metadata["Dimensions"] == [["Environment"]]
    assert set(payload) == {"_aws", "Environment", *METRICS}
    assert payload["Documents"] == 1 and payload["InputTokens"] == 900
    assert payload["CostUSD"] == .004 and payload["ValidationFailures"] == 0
    assert "Sensitive" not in lines[0] and "Jane" not in lines[0]


def test_emf_reports_review_ocr_and_error_paths():
    from claimlens.models import OcrInfo
    lines = []
    result = make_result("error")
    result.ocr = OcrInfo(engines=["test"], pages=3)
    result.usage.ocr_pages = 3
    Metrics("test", True, lines.append).record(result)
    payload = json.loads(lines[0])
    assert payload["ProcessingErrors"] == 1 and payload["OCRDocuments"] == 1 and payload["OCRPages"] == 3


def test_metrics_disabled_and_invalid_values_emit_nothing():
    lines = []
    Metrics("dev", False, lines.append).record(make_result())
    result = make_result()
    result.usage.cost_usd = float("nan")
    Metrics("dev", True, lines.append).record(result)
    assert lines == []
    with pytest.raises(ValueError):
        Metrics("private environment")


def test_telemetry_failures_do_not_break_processing():
    def broken(line):
        raise OSError("stdout unavailable")
    Metrics("dev", True, broken).record(make_result())


def test_duplicate_does_not_emit_or_charge_processing_metrics(session_factory, samples):
    lines = []
    llm = CountingLLM()
    settings = dataclasses.replace(get_settings(), metrics_enabled=True)
    service = ProcessingService(session_factory, llm, SqlIdempotencyStore(session_factory), settings=settings)
    service.metrics.sink = lines.append
    path = next(samples.glob("01_*"))
    service.ingest(path.read_bytes(), path.name, "test")
    service.ingest(path.read_bytes(), path.name, "test")
    assert len(lines) == 1 and llm.calls == 2


def test_transient_attempts_still_emit_cost_and_error_metrics(session_factory, samples):
    lines = []
    service = ProcessingService(session_factory, CountingLLM(fail_times=1), SqlIdempotencyStore(session_factory),
                                settings=dataclasses.replace(get_settings(), metrics_enabled=True))
    service.metrics.sink = lines.append
    path = next(samples.glob("01_*"))
    with pytest.raises(TransientProcessingError):
        service.ingest(path.read_bytes(), path.name, "test")
    assert json.loads(lines[0])["ProcessingErrors"] == 1
