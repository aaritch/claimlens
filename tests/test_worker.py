import io
import json
import logging
import threading
import time
import uuid

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import claimlens.worker as worker_mod
from claimlens.config import get_settings
from claimlens.llm.bedrock_client import LLMError
from claimlens.service import IngestOutcome, ProcessingService
from claimlens.storage import repository as repo
from claimlens.storage.db import Document, Upload
from claimlens.storage.idempotency import SqlIdempotencyStore
from claimlens.worker import Worker, backoff_seconds
from sqlalchemy import func, select
import dataclasses
from tests.test_service import CountingLLM

BUCKET = "docs-bucket"


def event(key, bucket=BUCKET, detail_type="Object Created"):
    return json.dumps({"version": "0", "detail-type": detail_type, "source": "aws.s3",
                       "detail": {"bucket": {"name": bucket}, "object": {"key": key, "size": 1}}})


def ukey(name="a.txt", upload_id=None):
    return f"incoming/{upload_id or uuid.uuid4()}/{name}"


# ---------------------------------------------------------------------------------------------------------
# Hand-written fakes: precise control over SQS/S3 behaviour, and we can assert on exact calls
# ---------------------------------------------------------------------------------------------------------
class FakeSqs:
    def __init__(self, bodies=(), receives=1, fail_receive=0):
        self.messages = [{"MessageId": f"m{i}", "ReceiptHandle": f"r{i}", "Body": b,
                          "Attributes": {"ApproximateReceiveCount": str(receives)}} for i, b in enumerate(bodies)]
        self.deleted, self.visibility, self.fail_receive, self.lock = [], [], fail_receive, threading.Lock()

    def receive_message(self, **kw):
        if self.fail_receive:
            self.fail_receive -= 1
            raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "x"}}, "ReceiveMessage")
        n = kw.get("MaxNumberOfMessages", 1)
        batch, self.messages = self.messages[:n], self.messages[n:]
        return {"Messages": batch} if batch else {}

    def delete_message(self, QueueUrl, ReceiptHandle):
        with self.lock:
            self.deleted.append(ReceiptHandle)

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        with self.lock:
            self.visibility.append((ReceiptHandle, VisibilityTimeout))


class FakeS3:
    def __init__(self, objects=None, tag_error=False):
        self.objects, self.tags, self.tag_error = dict(objects or {}), {}, tag_error

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "nf"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object_tagging(self, Bucket, Key, Tagging):
        if self.tag_error:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "PutObjectTagging")
        self.tags[Key] = {t["Key"]: t["Value"] for t in Tagging["TagSet"]}


@pytest.fixture
def doc_bytes(samples):
    return {p.name: p.read_bytes() for p in sorted(samples.glob("[0-9]*"))}


def build(session_factory, sqs, s3, llm=None, **settings_overrides):
    settings = dataclasses.replace(get_settings(), sqs_queue_url="q", queue_visibility_seconds=60, **settings_overrides)
    svc = ProcessingService(session_factory, llm or CountingLLM(), SqlIdempotencyStore(session_factory), settings=settings)
    return Worker(svc, sqs, s3, session_factory, settings, queue_url="q", concurrency=4, wait_seconds=0), svc


def n_docs(sf):
    with sf() as s:
        return s.scalar(select(func.count()).select_from(Document))


# ---- decisions per message ------------------------------------------------------------------------------------

def test_backoff_doubles_and_is_capped():
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert backoff_seconds(50) == 900 and backoff_seconds(0) == 30


def test_happy_path_processes_tags_marks_upload_and_deletes(session_factory, doc_bytes):
    name, data = next(iter(doc_bytes.items()))
    uid = str(uuid.uuid4())
    key = ukey(name, uid)
    with session_factory() as s:
        repo.create_upload(s, uid, key, name, "uma")
        s.commit()
    sqs, s3 = FakeSqs([event(key)]), FakeS3({key: data})
    w, svc = build(session_factory, sqs, s3)
    assert w.run_once() == 1
    assert sqs.deleted == ["r0"] and sqs.visibility == []
    with session_factory() as s:
        up = s.get(Upload, uid)
        assert up.state == "processed" and up.document_id
        assert s3.tags[key] == {"claimlens-status": "processed", "claimlens-document": up.document_id}
    assert n_docs(session_factory) == 1


def test_same_file_uploaded_twice_is_processed_once(session_factory, doc_bytes):
    name, data = next(iter(doc_bytes.items()))
    k1, k2 = ukey(name), ukey(name)
    llm = CountingLLM(delay=0.1)                      # overlap the two messages so they genuinely race
    sqs, s3 = FakeSqs([event(k1), event(k2)]), FakeS3({k1: data, k2: data})
    w, _ = build(session_factory, sqs, s3, llm=llm)
    w.run_once()
    # Guarantees that must hold however the race fell out:
    assert n_docs(session_factory) == 1 and llm.calls == 2          # one document, one model bill
    requeued = {rh for rh, delay in sqs.visibility if delay == worker_mod.IN_PROGRESS_RETRY_SECONDS}
    assert set(sqs.deleted) | requeued == {"r0", "r1"}               # nothing lost: each was finished or politely requeued
    # The requeued loser comes back later and is simply recognised as a duplicate.
    leftover = [k for rh, k in (("r0", k1), ("r1", k2)) if rh in requeued]
    again = FakeSqs([event(k) for k in leftover])
    w2, _ = build(session_factory, again, s3, llm=llm)
    w2.run_once()
    assert len(again.deleted) == len(leftover) and n_docs(session_factory) == 1 and llm.calls == 2


@pytest.mark.parametrize("body,reason", [
    ("not json at all", "malformed"),
    (json.dumps({"detail-type": "Object Deleted", "detail": {}}), "not an ObjectCreated event"),
    (json.dumps({"detail-type": "Object Created", "detail": {"bucket": {"name": BUCKET}}}), "no key"),
    (event("other-prefix/x.pdf"), "outside the upload prefix"),
    (event(ukey("ghost.pdf")), "object does not exist"),
])
def test_messages_that_can_never_succeed_are_deleted(session_factory, body, reason):
    sqs, s3 = FakeSqs([body]), FakeS3()
    w, _ = build(session_factory, sqs, s3)
    w.run_once()
    assert sqs.deleted == ["r0"], reason
    assert n_docs(session_factory) == 0


def test_oversize_object_is_quarantined_without_being_downloaded(session_factory):
    uid, key = str(uuid.uuid4()), None
    key = ukey("big.txt", uid)
    with session_factory() as s:
        repo.create_upload(s, uid, key, "big.txt", "uma")
        s.commit()
    s3 = FakeS3({key: b"x" * 500})
    s3.get_object = lambda **k: (_ for _ in ()).throw(AssertionError("must not download"))
    sqs = FakeSqs([event(key)])
    w, _ = build(session_factory, sqs, s3, max_upload_bytes=100)
    w.run_once()
    assert sqs.deleted == ["r0"] and s3.tags[key] == {"claimlens-status": "quarantined"}
    with session_factory() as s:
        assert s.get(Upload, uid).state == "failed" and s.get(Upload, uid).error == "too_large"


def test_unsupported_type_is_quarantined(session_factory):
    key = ukey("malware.exe")
    sqs, s3 = FakeSqs([event(key)]), FakeS3({key: b"MZ"})
    w, _ = build(session_factory, sqs, s3)
    w.run_once()
    assert sqs.deleted == ["r0"] and s3.tags[key]["claimlens-status"] == "quarantined"


def test_transient_failure_requeues_with_backoff_and_saves_nothing(session_factory, doc_bytes):
    name, data = next(iter(doc_bytes.items()))
    key = ukey(name)
    sqs, s3 = FakeSqs([event(key)], receives=3), FakeS3({key: data})
    w, _ = build(session_factory, sqs, s3, llm=CountingLLM(fail_times=99))
    w.run_once()
    assert sqs.deleted == [] and sqs.visibility[-1] == ("r0", 120)   # third delivery -> 30 * 2^2
    assert n_docs(session_factory) == 0 and key not in s3.tags


def test_in_progress_elsewhere_is_retried_soon(session_factory, doc_bytes, monkeypatch):
    name, data = next(iter(doc_bytes.items()))
    key = ukey(name)
    sqs, s3 = FakeSqs([event(key)]), FakeS3({key: data})
    w, svc = build(session_factory, sqs, s3)
    monkeypatch.setattr(svc, "ingest", lambda *a, **k: IngestOutcome("in_progress", None))
    w.run_once()
    assert sqs.deleted == [] and sqs.visibility[-1] == ("r0", worker_mod.IN_PROGRESS_RETRY_SECONDS)


def test_unexpected_crash_is_contained_and_never_logs_the_filename(session_factory, doc_bytes, monkeypatch, caplog):
    name, data = next(iter(doc_bytes.items()))
    key = ukey("PATIENT-JANE-DOE-claim.txt")
    sqs, s3 = FakeSqs([event(key)]), FakeS3({key: data})
    w, svc = build(session_factory, sqs, s3)
    monkeypatch.setattr(svc, "ingest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom PATIENT-JANE-DOE")))
    with caplog.at_level(logging.DEBUG, logger="claimlens.worker"):
        w.run_once()
    assert sqs.deleted == [] and sqs.visibility[-1][1] == 30
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged and "PATIENT" not in logged and "JANE" not in logged


def test_bookkeeping_failures_do_not_fail_the_document(session_factory, doc_bytes):
    name, data = next(iter(doc_bytes.items()))
    key = ukey(name)
    sqs, s3 = FakeSqs([event(key)]), FakeS3({key: data}, tag_error=True)
    w, _ = build(session_factory, sqs, s3)
    w.run_once()
    assert sqs.deleted == ["r0"] and n_docs(session_factory) == 1


def test_url_encoded_keys_fall_back_to_the_decoded_form(session_factory, doc_bytes):
    name, data = next(iter(doc_bytes.items()))
    real = ukey("my report.docx")
    encoded = real.replace(" ", "+")
    sqs, s3 = FakeSqs([event(encoded)]), FakeS3({real: data})
    w, _ = build(session_factory, sqs, s3)
    w.run_once()
    assert sqs.deleted == ["r0"] and s3.tags[real]["claimlens-status"] == "processed"


def test_a_literal_plus_in_a_real_key_wins_over_decoding(session_factory, doc_bytes):
    name, data = next(iter(doc_bytes.items()))
    real = ukey("a+b.docx")
    sqs, s3 = FakeSqs([event(real)]), FakeS3({real: data, real.replace("+", " "): b"decoy"})
    w, _ = build(session_factory, sqs, s3)
    w.run_once()
    assert real in s3.tags and real.replace("+", " ") not in s3.tags


def test_non_uuid_folder_is_processed_without_touching_uploads_table(session_factory, doc_bytes):
    name, data = next(iter(doc_bytes.items()))
    key = f"incoming/manual-drop/{name}"
    sqs, s3 = FakeSqs([event(key)]), FakeS3({key: data})
    w, _ = build(session_factory, sqs, s3)
    w.run_once()
    assert sqs.deleted == ["r0"] and n_docs(session_factory) == 1


# ---- heartbeat, concurrency, shutdown --------------------------------------------------------------------------------

def test_heartbeat_keeps_extending_visibility_during_a_long_job_then_stops(session_factory, doc_bytes):
    name, data = next(iter(doc_bytes.items()))
    key = ukey(name)
    sqs, s3 = FakeSqs([event(key)]), FakeS3({key: data})
    w, _ = build(session_factory, sqs, s3, llm=CountingLLM(delay=0.4))
    w.heartbeat_seconds = 0.1
    w.run_once()
    beats = [v for v in sqs.visibility if v == ("r0", 60)]
    assert len(beats) >= 2 and sqs.deleted == ["r0"]
    settled = len(sqs.visibility)
    time.sleep(0.3)
    assert len(sqs.visibility) == settled        # no zombie heartbeat after the job ended


def test_messages_are_processed_in_parallel_up_to_the_concurrency_limit(session_factory, doc_bytes):
    names = list(doc_bytes)[:6]
    keys = [ukey(n) for n in names]
    sqs = FakeSqs([event(k) for k in keys])
    s3 = FakeS3({k: doc_bytes[n] for k, n in zip(keys, names)})

    class Tracking(CountingLLM):
        active = peak = 0

        def call_tool(self, **kw):
            with self._lock:
                Tracking.active += 1
                Tracking.peak = max(Tracking.peak, Tracking.active)
            time.sleep(0.1)
            try:
                return super().call_tool(**kw)
            finally:
                with self._lock:
                    Tracking.active -= 1
    w, _ = build(session_factory, sqs, s3, llm=Tracking())
    w.concurrency = 3
    w._pool = worker_mod.ThreadPoolExecutor(max_workers=3)
    while w.run_once():
        pass
    assert len(sqs.deleted) == 6 and n_docs(session_factory) == 6
    assert 1 < Tracking.peak <= 3


def test_run_forever_stops_on_request_and_writes_a_liveness_file(session_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "LIVENESS_FILE", tmp_path / "alive")
    w, _ = build(session_factory, FakeSqs(), FakeS3())
    t = threading.Thread(target=w.run_forever)
    t.start()
    time.sleep(0.2)
    assert (tmp_path / "alive").exists()
    w.stop()
    t.join(timeout=5)
    assert not t.is_alive()


def test_queue_outage_does_not_kill_the_worker(session_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "LIVENESS_FILE", tmp_path / "alive")
    sqs = FakeSqs(fail_receive=1)
    w, _ = build(session_factory, sqs, FakeS3())
    w._stop.wait = lambda timeout=None: False        # skip the 5 s pause
    t = threading.Thread(target=w.run_forever)
    t.start()
    time.sleep(0.3)
    assert sqs.fail_receive == 0 and t.is_alive()    # survived the error and kept polling
    w.stop()
    t.join(timeout=5)


# ---------------------------------------------------------------------------------------------------------------------
# Integration against moto's real boto3 S3 + SQS
# ---------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        s3, sqs = boto3.client("s3", region_name="us-east-1"), boto3.client("sqs", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        dlq = sqs.create_queue(QueueName="dlq")["QueueUrl"]
        dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        q = sqs.create_queue(QueueName="main", Attributes={
            "VisibilityTimeout": "60", "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": 2})})["QueueUrl"]
        yield s3, sqs, q, dlq


def depth(sqs, url):
    a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    return int(a["ApproximateNumberOfMessages"]) + int(a["ApproximateNumberOfMessagesNotVisible"])


def test_real_s3_and_sqs_roundtrip(aws, session_factory, doc_bytes):
    s3, sqs, q, _ = aws
    name, data = next(iter(doc_bytes.items()))
    key = ukey(name)
    s3.put_object(Bucket=BUCKET, Key=key, Body=data)
    sqs.send_message(QueueUrl=q, MessageBody=event(key))
    settings = dataclasses.replace(get_settings(), sqs_queue_url=q, queue_visibility_seconds=60)
    svc = ProcessingService(session_factory, CountingLLM(), SqlIdempotencyStore(session_factory), settings=settings)
    w = Worker(svc, sqs, s3, session_factory, settings, wait_seconds=0)
    assert w.run_once() == 1
    tags = {t["Key"]: t["Value"] for t in s3.get_object_tagging(Bucket=BUCKET, Key=key)["TagSet"]}
    assert tags["claimlens-status"] == "processed" and "claimlens-document" in tags
    assert depth(sqs, q) == 0 and n_docs(session_factory) == 1


def test_poison_message_ends_up_in_the_dead_letter_queue(aws, session_factory, doc_bytes, monkeypatch):
    s3, sqs, q, dlq = aws
    name, data = next(iter(doc_bytes.items()))
    key = ukey(name)
    s3.put_object(Bucket=BUCKET, Key=key, Body=data)
    sqs.send_message(QueueUrl=q, MessageBody=event(key))
    monkeypatch.setattr(worker_mod, "backoff_seconds", lambda n: 0)        # let retries come back immediately
    settings = dataclasses.replace(get_settings(), sqs_queue_url=q, queue_visibility_seconds=60)
    svc = ProcessingService(session_factory, CountingLLM(), SqlIdempotencyStore(session_factory), settings=settings)
    monkeypatch.setattr(svc, "ingest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("always broken")))
    w = Worker(svc, sqs, s3, session_factory, settings, wait_seconds=0)
    for _ in range(4):
        w.run_once()
    assert depth(sqs, q) == 0 and depth(sqs, dlq) == 1      # SQS itself moved it after maxReceiveCount deliveries
    assert n_docs(session_factory) == 0
