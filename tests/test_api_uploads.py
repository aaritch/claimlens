import base64
import dataclasses
import json

import boto3
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from claimlens.api.app import create_app
from claimlens.api.auth import Authenticator
from claimlens.config import get_settings
from claimlens.llm.mock import MockLLM
from claimlens.service import ProcessingService
from claimlens.storage.db import AuditLog
from claimlens.storage.idempotency import SqlIdempotencyStore
from claimlens.worker import Worker
from tests.test_api import H, TOKENS
from tests.test_worker import FakeS3, FakeSqs, event


@pytest.fixture
def stack(session_factory, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTTESTTESTTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    def make(**overrides):
        values = dict(documents_bucket="docs-bucket", sync_upload_enabled=False, max_upload_bytes=5_000_000,
                      sqs_queue_url="q", queue_visibility_seconds=60)
        values.update(overrides)
        settings = dataclasses.replace(get_settings(), **values)
        svc = ProcessingService(session_factory, MockLLM("careful"), SqlIdempotencyStore(session_factory), settings=settings)
        s3 = boto3.client("s3", region_name="us-east-1")
        app = create_app(svc, session_factory, settings, Authenticator(TOKENS), s3_client=s3)
        return TestClient(app), svc, settings
    return make


def ticket(client, name="claim.pdf", who="upload"):
    return client.post("/uploads", json={"filename": name}, headers=H[who])


def test_ticket_points_at_a_unique_prefixed_key_with_a_size_limit(stack):
    client, _, settings = stack()
    r = ticket(client, "../../My Claim.pdf")
    assert r.status_code == 201
    t = r.json()
    assert t["fields"]["key"] == f"incoming/{t['upload_id']}/My Claim.pdf"        # traversal stripped, id-scoped folder
    assert "docs-bucket" in t["url"] and t["expires_in"] == settings.upload_url_ttl_seconds
    policy = json.loads(base64.b64decode(t["fields"]["policy"]))
    assert ["content-length-range", 1, 5_000_000] in policy["conditions"]          # S3 itself enforces the limit
    assert {"bucket": "docs-bucket"} in policy["conditions"]


def test_every_ticket_gets_its_own_folder_and_is_audited(stack, session_factory):
    client, _, _ = stack()
    a, b = ticket(client).json(), ticket(client).json()
    assert a["upload_id"] != b["upload_id"] and a["fields"]["key"] != b["fields"]["key"]
    with session_factory() as s:
        rows = s.scalars(select(AuditLog).where(AuditLog.action == "upload_requested")).all()
        assert len(rows) == 2 and {r.actor for r in rows} == {"uma"}


@pytest.mark.parametrize("name,who,code", [("virus.exe", "upload", 415), ("a.pdf", "reader", 403)])
def test_ticket_rejections(stack, name, who, code):
    client, _, _ = stack()
    assert ticket(client, name, who).status_code == code


def test_unconfigured_server_says_so(stack):
    client, _, _ = stack(documents_bucket="")
    assert ticket(client).status_code == 503


def test_synchronous_upload_route_disappears_when_disabled(stack):
    client, _, _ = stack()
    assert client.post("/documents", files={"file": ("a.txt", b"x")}, headers=H["upload"]).status_code == 405
    assert "post" not in client.get("/openapi.json").json()["paths"]["/documents"]


def test_upload_status_is_private_to_the_requester_but_visible_to_reviewers(stack):
    client, _, _ = stack()
    uid = ticket(client).json()["upload_id"]
    assert client.get(f"/uploads/{uid}", headers=H["upload"]).json()["state"] == "pending"
    assert client.get(f"/uploads/{uid}", headers=H["review"]).status_code == 200
    assert client.get(f"/uploads/{uid}", headers=H["reader"]).status_code == 403       # readers cannot use the upload API at all
    assert client.get("/uploads/not-a-real-id", headers=H["upload"]).status_code == 404


def test_full_loop_ticket_upload_queue_worker_status_document(stack, session_factory, samples):
    """The production flow end to end, with S3/SQS faked at the edges."""
    client, svc, settings = stack()
    path = next(samples.glob("01_*"))
    t = ticket(client, path.name).json()
    key = t["fields"]["key"]
    s3 = FakeS3({key: path.read_bytes()})                      # the browser POSTs the file straight to S3...
    sqs = FakeSqs([event(key)])                                # ...S3 -> EventBridge -> SQS delivers this message
    Worker(svc, sqs, s3, session_factory, settings, queue_url="q", wait_seconds=0).run_once()
    status = client.get(f"/uploads/{t['upload_id']}", headers=H["upload"]).json()
    assert status["state"] == "processed" and status["document_id"]
    doc = client.get(f"/documents/{status['document_id']}", headers=H["reader"]).json()
    assert doc["status"] == "validated" and doc["claim"]["provider_npi"]
    assert sqs.deleted == ["r0"]
    # Uploading the very same file again resolves to the same document.
    t2 = ticket(client, path.name).json()
    s3.objects[t2["fields"]["key"]] = path.read_bytes()
    Worker(svc, FakeSqs([event(t2["fields"]["key"])]), s3, session_factory, settings, queue_url="q", wait_seconds=0).run_once()
    again = client.get(f"/uploads/{t2['upload_id']}", headers=H["upload"]).json()
    assert again["state"] == "duplicate" and again["document_id"] == status["document_id"]
