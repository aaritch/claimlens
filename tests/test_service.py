import threading
import time

import fakeredis
import pytest
import redis
from sqlalchemy import func, select

from claimlens.config import get_settings
from claimlens.llm.bedrock_client import LLMError
from claimlens.llm.mock import MockLLM
from claimlens.service import ProcessingService, TransientProcessingError, UnsupportedUpload, safe_basename
from claimlens.storage import repository as repo
from claimlens.storage.cache import InMemoryCache, RedisCache
from claimlens.storage.db import AuditLog, Document
from claimlens.storage.idempotency import FAILED, SqlIdempotencyStore


class CountingLLM(MockLLM):
    """Counts model calls (= money). Optional delay widens race windows; fail_times simulates throttling."""

    def __init__(self, delay=0.0, fail_times=0):
        super().__init__("careful")
        self.calls, self.delay, self.fail_times, self._lock = 0, delay, fail_times, threading.Lock()

    def call_tool(self, **kw):
        with self._lock:
            self.calls += 1
            n = self.calls
        if self.delay:
            time.sleep(self.delay)
        if n <= self.fail_times:
            raise LLMError("Bedrock call failed: ThrottlingException", transient=True)
        return super().call_tool(**kw)


@pytest.fixture
def doc_bytes(samples):
    return {p.name: p.read_bytes() for p in sorted(samples.glob("[0-9]*"))}


def build(session_factory, llm=None, cache=None, **kw):
    llm = llm or CountingLLM()
    idem = SqlIdempotencyStore(session_factory)
    return ProcessingService(session_factory, llm, idem, cache=cache, settings=get_settings(), **kw), llm, idem


def count_docs(sf):
    with sf() as s:
        return s.scalar(select(func.count()).select_from(Document))


def test_first_upload_creates_second_is_a_free_duplicate(session_factory, doc_bytes):
    svc, llm, _ = build(session_factory)
    name, data = next(iter(doc_bytes.items()))
    first = svc.ingest(data, name, "alice")
    assert first.outcome == "created" and llm.calls == 2          # classify + extract
    again = svc.ingest(data, "renamed-copy.docx", "bob")
    assert again.outcome == "duplicate" and again.document_id == first.document_id
    assert llm.calls == 2 and count_docs(session_factory) == 1     # no second bill, no second row


def test_result_is_stored_and_audited(session_factory, doc_bytes):
    svc, _, _ = build(session_factory)
    name, data = next(iter(doc_bytes.items()))
    out = svc.ingest(data, name, "alice")
    with session_factory() as s:
        d = repo.get_document(s, out.document_id)
        assert d.status == "validated" and d.provider_npi and d.cost_usd == 0 and d.model_id == "mock-careful"
        log = s.scalars(select(AuditLog)).one()
        assert (log.actor, log.action, log.document_id) == ("alice", "ingest", d.id)


def test_redis_fast_path_skips_database_and_lease(session_factory, doc_bytes):
    cache = RedisCache(client=fakeredis.FakeRedis())
    svc, llm, idem = build(session_factory, cache=cache)
    name, data = next(iter(doc_bytes.items()))
    first = svc.ingest(data, name, "a")
    calls = {"begin": 0}
    real_begin = idem.begin
    idem.begin = lambda *a, **k: (calls.__setitem__("begin", calls["begin"] + 1), real_begin(*a, **k))[1]
    assert svc.ingest(data, name, "a").document_id == first.document_id
    assert calls["begin"] == 0 and llm.calls == 2


def test_service_works_when_redis_is_down(session_factory, doc_bytes):
    class Down:
        def get(self, *a, **k): raise redis.ConnectionError("down")
        def set(self, *a, **k): raise redis.ConnectionError("down")
    svc, llm, _ = build(session_factory, cache=RedisCache(client=Down()))
    name, data = next(iter(doc_bytes.items()))
    assert svc.ingest(data, name, "a").outcome == "created"
    assert svc.ingest(data, name, "a").outcome == "duplicate" and llm.calls == 2   # DB still dedupes


def test_changing_the_recipe_reprocesses_the_same_file(session_factory, doc_bytes):
    name, data = next(iter(doc_bytes.items()))
    v1, llm1, _ = build(session_factory, extract_prompt="extract_v1")
    v2, llm2, _ = build(session_factory, extract_prompt="extract_v2")
    assert v1.fingerprint != v2.fingerprint
    a, b = v1.ingest(data, name, "x"), v2.ingest(data, name, "x")
    assert a.outcome == b.outcome == "created" and a.document_id != b.document_id
    assert count_docs(session_factory) == 2 and llm2.calls == 2


def test_editing_a_prompt_file_changes_the_fingerprint(session_factory, monkeypatch):
    svc, _, _ = build(session_factory)
    before = svc.fingerprint
    import claimlens.service as mod
    original = mod.load_prompt
    monkeypatch.setattr(mod, "load_prompt", lambda name: original(name) + " (edited)")
    assert svc.fingerprint != before


def test_fingerprint_changes_with_model_ocr_and_rules(session_factory, monkeypatch):
    a, _, _ = build(session_factory)
    b, _, _ = build(session_factory, llm=type("M", (MockLLM,), {})("naive"))
    c, _, _ = build(session_factory, ocr_spec="tesseract")
    assert len({a.fingerprint, b.fingerprint, c.fingerprint}) == 3
    import claimlens.service as mod
    monkeypatch.setattr(mod, "RULES_VERSION", "999")
    assert a.fingerprint not in {a2 for a2 in [b.fingerprint, c.fingerprint]}


def test_eight_simultaneous_identical_uploads_process_exactly_once(session_factory, doc_bytes):
    svc, llm, _ = build(session_factory, llm=CountingLLM(delay=0.15))
    name, data = next(iter(doc_bytes.items()))
    outcomes, errors = [], []

    def worker():
        try:
            outcomes.append(svc.ingest(data, name, "w").outcome)
        except Exception as exc:  # pragma: no cover - would fail the assertions below
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert outcomes.count("created") == 1 and set(outcomes) <= {"created", "duplicate", "in_progress"}
    assert llm.calls == 2 and count_docs(session_factory) == 1      # one document, one model bill, no matter the stampede
    assert svc.ingest(data, name, "w").outcome == "duplicate"


def test_transient_failure_saves_nothing_and_allows_a_clean_retry(session_factory, doc_bytes):
    svc, llm, _ = build(session_factory, llm=CountingLLM(fail_times=1))
    name, data = next(iter(doc_bytes.items()))
    with pytest.raises(TransientProcessingError):
        svc.ingest(data, name, "a")
    assert count_docs(session_factory) == 0                          # the failure was not recorded as an answer
    retry = svc.ingest(data, name, "a")
    assert retry.outcome == "created" and count_docs(session_factory) == 1


def test_permanent_failure_is_recorded_once_not_retried_forever(session_factory):
    svc, llm, _ = build(session_factory)
    out = svc.ingest(b"definitely not a pdf", "broken.pdf", "a")
    with session_factory() as s:
        d = repo.get_document(s, out.document_id)
        assert (d.status, d.error_kind) == ("error", "permanent") and "unreadable PDF" in d.error
    assert svc.ingest(b"definitely not a pdf", "broken.pdf", "a").outcome == "duplicate" and llm.calls == 0


def test_unexpected_crash_releases_the_lease_for_retry(session_factory, doc_bytes, monkeypatch):
    svc, _, idem = build(session_factory)
    name, data = next(iter(doc_bytes.items()))
    monkeypatch.setattr(repo, "save_result", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db exploded")))
    with pytest.raises(RuntimeError):
        svc.ingest(data, name, "a")
    import hashlib
    key = f"{hashlib.sha256(data).hexdigest()}:{svc.fingerprint}"
    assert idem.begin(key, "someone-else", 60).acquired               # FAILED leases can be re-taken immediately


def test_losing_a_write_race_is_handled_by_the_unique_constraint(session_factory, doc_bytes, monkeypatch):
    """Both earlier checks miss (e.g. a lagging replica); the database's UNIQUE constraint is the last line of defence."""
    name, data = next(iter(doc_bytes.items()))
    winner, _, _ = build(session_factory)
    existing = winner.ingest(data, name, "first").document_id
    svc, llm, _ = build(session_factory)
    real, state = repo.find_by_hash, {"n": 0}

    def flaky_find(session, sha, fp):
        state["n"] += 1
        return None if state["n"] == 1 else real(session, sha, fp)   # blind the first pre-check only
    monkeypatch.setattr(repo, "find_by_hash", flaky_find)
    out = svc.ingest(data, name, "second")
    assert out.outcome == "duplicate" and out.document_id == existing and count_docs(session_factory) == 1


def test_upload_validation_and_filename_hygiene(session_factory):
    svc, _, _ = build(session_factory)
    with pytest.raises(UnsupportedUpload):
        svc.ingest(b"x", "malware.exe", "a")
    assert safe_basename("../../etc/passwd.pdf") == "passwd.pdf"
    assert safe_basename("C:\\Users\\x\\scan.PDF") == "scan.PDF"
    assert safe_basename("bad\x00name\n.pdf") == "badname.pdf"
    assert safe_basename("") == "upload"
