"""One contract, run against BOTH backends: if they ever drift apart these tests will say so."""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest
from moto import mock_aws

from claimlens.storage.idempotency import (DONE, FAILED, IN_PROGRESS, DynamoIdempotencyStore, SqlIdempotencyStore,
                                           create_dynamodb_table)


class SerializedTable:
    """moto's emulator evaluates a condition and THEN writes without holding a lock (shown experimentally:
    even the simplest `attribute_not_exists(pk)` update let two threads win in ~1 of 40 races). Real DynamoDB
    makes this atomic. Serializing calls here means the tests check OUR condition logic. They cannot prove
    DynamoDB's atomicity; test_true_dynamodb_race below does that against DynamoDB Local / real AWS."""

    def __init__(self, table):
        self._t, self._lock = table, threading.Lock()

    def __getattr__(self, name):
        attr = getattr(self._t, name)
        if not callable(attr):
            return attr

        def locked(*a, **k):
            with self._lock:
                return attr(*a, **k)
        return locked


@pytest.fixture(params=["sql", "dynamodb"])
def store(request, session_factory, monkeypatch):
    if request.param == "sql":
        yield SqlIdempotencyStore(session_factory)
        return
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        create_dynamodb_table("idem", resource=ddb)
        dyn = DynamoIdempotencyStore("idem", resource=ddb)
        dyn._table = SerializedTable(dyn._table)
        yield dyn


def test_first_caller_wins_second_is_told_in_progress(store):
    assert store.begin("k", "A", 60, now=1000).acquired
    second = store.begin("k", "B", 60, now=1010)
    assert not second.acquired and second.status == IN_PROGRESS


def test_completed_work_is_never_redone_and_points_to_the_result(store):
    store.begin("k", "A", 60, now=1000)
    assert store.complete("k", "A", "doc-1", now=1005)
    later = store.begin("k", "B", 60, now=999_999)  # even long after the lease would have expired
    assert not later.acquired and later.status == DONE and later.document_id == "doc-1"


def test_failed_work_can_be_retried_by_anyone(store):
    store.begin("k", "A", 60, now=1000)
    assert store.fail("k", "A", "ThrottlingException", now=1001)
    retry = store.begin("k", "B", 60, now=1002)
    assert retry.acquired


def test_expired_lease_can_be_taken_over_and_the_old_owner_is_fenced_out(store):
    store.begin("k", "A", 60, now=1000)
    assert not store.begin("k", "B", 60, now=1059).acquired      # lease still valid
    assert store.begin("k", "B", 60, now=1061).acquired          # expired: B takes over
    assert store.complete("k", "A", "stale-doc", now=1062) is False  # A wakes up late: refused
    assert store.fail("k", "A", "late", now=1062) is False
    assert store.complete("k", "B", "good-doc", now=1063) is True
    assert store.begin("k", "C", 60, now=2000).document_id == "good-doc"


def test_only_the_owner_can_finish(store):
    store.begin("k", "A", 60, now=1000)
    assert store.complete("k", "intruder", "x", now=1001) is False


def test_keys_are_independent(store):
    assert store.begin("k1", "A", 60, now=1000).acquired and store.begin("k2", "A", 60, now=1000).acquired


def test_sixteen_simultaneous_workers_produce_exactly_one_winner(store):
    with ThreadPoolExecutor(16) as pool:
        leases = list(pool.map(lambda i: store.begin("race", f"w{i}", 300), range(16)))
    assert sum(l.acquired for l in leases) == 1


@pytest.mark.skipif(not os.getenv("CLAIMLENS_TEST_DDB_ENDPOINT"),
                    reason="set CLAIMLENS_TEST_DDB_ENDPOINT=http://localhost:8000 (DynamoDB Local) to run the true race")
def test_true_dynamodb_race(monkeypatch):
    """Run this against a REAL DynamoDB implementation to verify the atomicity guarantee we rely on."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    endpoint, table = os.environ["CLAIMLENS_TEST_DDB_ENDPOINT"], f"idem_{uuid.uuid4().hex[:8]}"
    create_dynamodb_table(table, endpoint_url=endpoint)
    store = DynamoIdempotencyStore(table, endpoint_url=endpoint)
    for round_no in range(20):
        with ThreadPoolExecutor(16) as pool:
            leases = list(pool.map(lambda i: store.begin(f"race{round_no}", f"w{i}", 300), range(16)))
        assert sum(l.acquired for l in leases) == 1
