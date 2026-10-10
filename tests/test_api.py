import dataclasses
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from claimlens.api.app import create_app
from claimlens.api.auth import AuthConfigError, Authenticator
from claimlens.config import get_settings
from claimlens.eval.runner import load_ground_truth
from claimlens.llm.bedrock_client import LLMError
from claimlens.llm.mock import MockLLM
from claimlens.service import IngestOutcome, ProcessingService
from claimlens.storage import repository as repo
from claimlens.storage.db import AuditLog, Document
from claimlens.storage.idempotency import SqlIdempotencyStore

TOKENS = ("reader-token-0123456789:rita_reader:reader,upload-token-0123456789:uma:uploader,"
          "review-token-0123456789:rob:reviewer,admin-token-0123456789:ada:admin")
H = {r: {"Authorization": f"Bearer {r}-token-0123456789"} for r in ("reader", "upload", "review", "admin")}

REVIEW_ME = b"Provider Appeal of payment determination\nPayer: Aetna\nWe dispute the payment.\n"  # extraction will be incomplete


def distinct_incomplete(i: int) -> tuple[str, bytes]:
    return f"appeal_{i}.txt", REVIEW_ME + f"Reference note {i}\n".encode()


class AlwaysTransient(MockLLM):
    def call_tool(self, **kw):
        raise LLMError("Bedrock call failed: ThrottlingException", transient=True)


@pytest.fixture
def make_api(session_factory):
    def _make(llm=None, settings=None, enable_docs=True):
        settings = settings or get_settings()
        llm = llm or MockLLM("careful")
        svc = ProcessingService(session_factory, llm, SqlIdempotencyStore(session_factory), settings=settings)
        app = create_app(svc, session_factory, settings, Authenticator(TOKENS), enable_docs=enable_docs)
        return TestClient(app), svc
    return _make


@pytest.fixture
def api(make_api):
    return make_api()


def upload(client, name, data, who="upload"):
    return client.post("/documents", files={"file": (name, data)}, headers=H[who])


def upload_incomplete(client, i=0):
    name, data = distinct_incomplete(i)
    r = upload(client, name, data)
    assert r.status_code == 201 and r.json()["document"]["status"] == "needs_review", r.text
    return r.json()["document"]


# ---- auth ------------------------------------------------------------------------------------------

def test_health_needs_no_auth_and_readiness_checks_the_database(api):
    client, _ = api
    assert client.get("/healthz").json() == {"status": "ok"} and client.get("/readyz").status_code == 200


def test_readiness_fails_when_the_database_is_down(session_factory):
    class Broken:
        def __call__(self):
            raise RuntimeError("db down")
    svc = ProcessingService(session_factory, MockLLM(), SqlIdempotencyStore(session_factory), settings=get_settings())
    client = TestClient(create_app(svc, Broken(), get_settings(), Authenticator(TOKENS)))
    assert client.get("/readyz").status_code == 503


def test_missing_and_wrong_tokens_are_401(api):
    client, _ = api
    assert client.get("/documents").status_code == 401
    assert client.get("/documents", headers={"Authorization": "Bearer nope-nope-nope-nope"}).status_code == 401
    assert client.get("/documents", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.get("/documents").headers["www-authenticate"] == "Bearer"


def test_roles_are_enforced(api):
    client, _ = api
    assert client.get("/documents", headers=H["reader"]).status_code == 200
    assert upload(client, "a.txt", b"hello", who="reader").status_code == 403
    assert client.post("/documents/x/review", json={"decision": "reject", "expected_version": 1, "note": "no"},
                       headers=H["upload"]).status_code == 403
    assert client.post("/documents/x/review", json={"decision": "reject", "expected_version": 1, "note": "no"},
                       headers=H["review"]).status_code == 404  # allowed in, but nothing there


@pytest.mark.parametrize("spec,msg", [("", "no API tokens"), ("short:bob:reader", "shorter than"),
                                      ("long-enough-token-123:bob:superuser", "token:username:role"),
                                      ("malformed", "token:username:role")])
def test_api_refuses_to_start_insecurely_configured(spec, msg):
    with pytest.raises(AuthConfigError, match=msg):
        Authenticator(spec)


def test_auth_can_be_explicitly_disabled_for_local_dev():
    assert Authenticator("", disabled=True).authenticate(None).role == "admin"


# ---- upload ------------------------------------------------------------------------------------------

def test_upload_then_duplicate_then_headers(api, samples):
    client, svc = api
    path = next(samples.glob("01_*"))
    first = upload(client, path.name, path.read_bytes())
    assert first.status_code == 201 and first.json()["outcome"] == "created"
    doc = first.json()["document"]
    assert first.headers["location"] == f"/documents/{doc['id']}"
    assert doc["status"] == "validated" and doc["claim"]["provider_npi"] and doc["version"] == 1
    assert first.headers["cache-control"] == "no-store" and first.headers["x-content-type-options"] == "nosniff"
    again = upload(client, "renamed.docx", path.read_bytes())
    assert again.status_code == 200 and again.json()["outcome"] == "duplicate" and again.json()["document"]["id"] == doc["id"]


def test_response_contains_extracted_fields_but_never_document_text(api, samples):
    client, _ = api
    path = next(samples.glob("01_*"))
    body = upload(client, path.name, path.read_bytes()).text
    # extracted fields are returned; the document's own sentences and any text-ish keys are not
    assert "I am submitting a complaint" not in body and '"text"' not in body and '"content"' not in body


@pytest.mark.parametrize("name,data,code", [("evil.exe", b"MZ", 415), ("empty.txt", b"", 400)])
def test_upload_rejections(api, name, data, code):
    client, _ = api
    assert upload(client, name, data).status_code == code


def test_oversized_upload_is_413(make_api):
    client, _ = make_api(settings=dataclasses.replace(get_settings(), max_upload_bytes=100))
    assert upload(client, "big.txt", b"x" * 500).status_code == 413


def test_in_progress_is_202_with_retry_after(api, monkeypatch):
    client, svc = api
    monkeypatch.setattr(svc, "ingest", lambda *a, **k: IngestOutcome("in_progress", None))
    r = upload(client, "a.txt", b"hello")
    assert r.status_code == 202 and r.headers["retry-after"] and r.json()["document"] is None


def test_transient_failure_is_503_with_retry_after_and_nothing_saved(make_api, session_factory):
    client, _ = make_api(llm=AlwaysTransient())
    name, data = distinct_incomplete(1)
    r = upload(client, name, data)
    assert r.status_code == 503 and "retry-after" in r.headers
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Document)) == 0


def test_hostile_filename_is_neutralised(api):
    client, _ = api
    name, data = distinct_incomplete(2)
    r = upload(client, "../../etc/passwd.txt", data)
    name = r.json()["document"]["source_name"]
    assert name.endswith(".txt") and len(name) == 36 and "passwd" not in name


def test_concurrent_identical_uploads_create_one_document(make_api, session_factory):
    client, svc = make_api()
    name, data = distinct_incomplete(3)
    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(lambda _: upload(client, name, data).status_code, range(6)))
    assert codes.count(201) == 1 and set(codes) <= {200, 201, 202}
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Document)) == 1


# ---- reading / listing ---------------------------------------------------------------------------------

def test_get_document_404_and_audited_view(api, session_factory):
    client, _ = api
    assert client.get("/documents/nope", headers=H["reader"]).status_code == 404
    doc = upload_incomplete(client)
    assert client.get(f"/documents/{doc['id']}", headers=H["reader"]).json()["id"] == doc["id"]
    with session_factory() as s:
        views = s.scalars(select(AuditLog).where(AuditLog.action == "view")).all()
        assert [(v.actor, v.document_id) for v in views] == [("rita_reader", doc["id"])]


def test_list_filter_and_paginate(api):
    client, _ = api
    ids = [upload_incomplete(client, i)["id"] for i in range(5)]
    seen, cursor = [], None
    for _ in range(10):
        r = client.get("/documents", params={"limit": 2, **({"cursor": cursor} if cursor else {})}, headers=H["reader"]).json()
        seen += [d["id"] for d in r["items"]]
        cursor = r["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(ids) and len(seen) == 5
    assert len(client.get("/claims", headers=H["reader"]).json()["items"]) == 5          # default: the review queue
    assert client.get("/documents", params={"status": "validated"}, headers=H["reader"]).json()["items"] == []


@pytest.mark.parametrize("params,code", [({"status": "bogus"}, 422), ({"limit": 0}, 422), ({"limit": 999}, 422),
                                         ({"cursor": "garbage"}, 400)])
def test_list_input_validation(api, params, code):
    client, _ = api
    assert client.get("/documents", params=params, headers=H["reader"]).status_code == code


# ---- review workflow ---------------------------------------------------------------------------------------

def test_review_approve_with_corrections_end_to_end(api, session_factory, tmp_path):
    client, _ = api
    doc = upload_incomplete(client)
    assert {f["rule"] for f in doc["findings"]} >= {"required_field"}
    fix = {"provider_npi": "1234567893", "disputed_amount": 2200.5, "claim_reference": "CLM-555555"}
    r = client.post(f"/documents/{doc['id']}/review", headers=H["review"],
                    json={"decision": "approve", "expected_version": doc["version"], "corrections": fix})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "validated" and out["reviewed_by"] == "rob" and out["version"] == 2
    assert out["claim"]["disputed_amount"] == 2200.5 and out["claim"]["provider_npi"] == "1234567893"
    assert client.get("/claims", headers=H["reader"]).json()["items"] == []        # left the queue
    # the loop closes: the human fix becomes an evaluation answer key
    with session_factory() as s:
        assert repo.export_ground_truth(s, tmp_path) == 1
    assert load_ground_truth(tmp_path)[0]["expected"]["claim_reference"] == "CLM-555555"


def test_review_conflicts_and_state_rules(api):
    client, _ = api
    doc = upload_incomplete(client)
    url, fix = f"/documents/{doc['id']}/review", {"provider_npi": "1234567893", "disputed_amount": 10, "claim_reference": "CLM-111111"}
    stale = client.post(url, headers=H["review"], json={"decision": "approve", "expected_version": 7, "corrections": fix})
    assert stale.status_code == 409
    ok = client.post(url, headers=H["review"], json={"decision": "approve", "expected_version": 1, "corrections": fix})
    assert ok.status_code == 200
    again = client.post(url, headers=H["admin"], json={"decision": "approve", "expected_version": 2})
    assert again.status_code == 409 and "cannot be reviewed" in again.json()["detail"]


def test_review_validation_errors_are_422_and_never_echo_values(api):
    client, _ = api
    doc = upload_incomplete(client)
    url = f"/documents/{doc['id']}/review"
    bad_npi = client.post(url, headers=H["review"], json={"decision": "approve", "expected_version": 1,
                                                          "corrections": {"provider_npi": "9999999999", "disputed_amount": 5, "claim_reference": "CLM-1"}})
    assert bad_npi.status_code == 422 and "npi_valid" in bad_npi.text and "9999999999" not in bad_npi.text
    junk = client.post(url, headers=H["review"], json={"decision": "approve", "expected_version": 1,
                                                       "corrections": {"disputed_amount": "TOPSECRET"}})
    assert junk.status_code == 422 and "TOPSECRET" not in junk.text
    no_note = client.post(url, headers=H["review"], json={"decision": "reject", "expected_version": 1})
    assert no_note.status_code == 422
    assert client.post(url, headers=H["review"], json={"decision": "maybe", "expected_version": 1}).status_code == 422
    assert client.get(f"/documents/{doc['id']}", headers=H["reader"]).json()["status"] == "needs_review"   # nothing changed


def test_review_reject_with_note_and_audit_trail(api, session_factory):
    client, _ = api
    doc = upload_incomplete(client)
    r = client.post(f"/documents/{doc['id']}/review", headers=H["review"],
                    json={"decision": "reject", "expected_version": 1, "note": "duplicate of claim on file"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    with session_factory() as s:
        actions = [(a.actor, a.action) for a in s.scalars(select(AuditLog).order_by(AuditLog.id))]
        assert actions == [("uma", "ingest"), ("rob", "review_reject")]


# ---- docs -----------------------------------------------------------------------------------------------------

def test_openapi_lists_the_resources_and_can_be_disabled(make_api):
    on, _ = make_api()
    paths = on.get("/openapi.json").json()["paths"]
    assert {"/documents", "/documents/{document_id}", "/documents/{document_id}/review", "/claims"} <= set(paths)
    off, _ = make_api(enable_docs=False)
    assert off.get("/docs").status_code == 404 and off.get("/openapi.json").status_code == 404
