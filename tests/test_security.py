import dataclasses
import json
import logging
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from claimlens.api.auth import AuthConfigError, OIDCAuthenticator
from claimlens.logging_safe import SafeLogFilter
from claimlens.pipeline import process_file
from claimlens.llm.mock import MockLLM
from tests.test_api import H, make_api, upload, upload_incomplete


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def oidc(key):
    jwks = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key()))
    return OIDCAuthenticator("https://identity.example", "claimlens", "https://identity.example/jwks", jwks_client=jwks)


def token(key, **overrides):
    now = int(time.time())
    claims = {"iss": "https://identity.example", "aud": "claimlens", "sub": "subject-123",
              "exp": now + 300, "iat": now, "cognito:groups": ["reader", "reviewer"]}
    claims.update(overrides)
    return "Bearer " + jwt.encode(claims, key, algorithm="RS256")


def test_oidc_maps_only_known_roles(signing_key):
    actor = oidc(signing_key).authenticate(token(signing_key))
    assert actor.name == "subject-123" and actor.role == "reviewer"
    assert not actor.allows("admin")


@pytest.mark.parametrize("claims", [
    {"aud": "wrong"}, {"iss": "https://wrong.example"}, {"exp": 1}, {"iat": int(time.time()) + 3600},
    {"sub": None},
])
def test_oidc_rejects_invalid_claims(signing_key, claims):
    with pytest.raises(HTTPException) as exc:
        oidc(signing_key).authenticate(token(signing_key, **claims))
    assert exc.value.status_code == 401


@pytest.mark.parametrize("roles", [[], ["superuser"], {"admin": True}, [None]])
def test_oidc_never_defaults_unknown_roles_to_reader(signing_key, roles):
    with pytest.raises(HTTPException) as exc:
        oidc(signing_key).authenticate(token(signing_key, **{"cognito:groups": roles}))
    assert exc.value.status_code == 403


def test_oidc_rejects_other_signatures_and_algorithms(signing_key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(HTTPException):
        oidc(signing_key).authenticate(token(other))
    with pytest.raises(HTTPException):
        oidc(signing_key).authenticate("Bearer " + jwt.encode({"sub": "admin"}, "a" * 32, algorithm="HS256"))


@pytest.mark.parametrize("url", ["http://identity.example/jwks", "https://user:secret@identity.example/jwks"])
def test_oidc_requires_secure_pinned_urls(url):
    with pytest.raises(AuthConfigError):
        OIDCAuthenticator("https://identity.example", "aud", url)


def test_oidc_provider_outage_is_retryable(signing_key):
    auth = oidc(signing_key)
    def unavailable(token):
        raise jwt.PyJWKClientConnectionError("sensitive remote details")
    auth.jwks.get_signing_key_from_jwt = unavailable
    with pytest.raises(HTTPException) as exc:
        auth.authenticate(token(signing_key))
    assert exc.value.status_code == 503 and "sensitive" not in exc.value.detail


def test_safe_logging_drops_free_text_secret_fields_and_tracebacks():
    record = logging.LogRecord("third.party", logging.ERROR, __file__, 1,
                               "Patient Jane Example SSN %s Bearer private-token", ("123-45-6789",),
                               (ValueError, ValueError("secret database password"), None))
    record.event = "processing_finished"
    record.fields = {"status": "validated", "cost_usd": .004, "patient": "Jane Example",
                     "document_text": "private", "token": "private-token", "elapsed_ms": float("inf")}
    assert SafeLogFilter().filter(record)
    payload = json.loads(record.getMessage())
    assert payload["status"] == "validated" and payload["cost_usd"] == .004
    assert not any(word in record.getMessage() for word in ("Jane", "123-45", "private", "password", "elapsed_ms"))
    assert record.exc_info is None and record.args == ()


def test_logging_rejects_untrusted_event_names():
    record = logging.LogRecord("name", logging.INFO, __file__, 1, "Jane Example", (), None)
    record.event = "Jane Example 123-45-6789"
    SafeLogFilter().filter(record)
    assert json.loads(record.getMessage())["event"] == "unstructured_log_suppressed"

def test_logging_does_not_treat_a_lowercase_name_as_an_event():
    record = logging.LogRecord("name", logging.INFO, __file__, 1, "", (), None)
    record.event = "patient_jane_example"
    SafeLogFilter().filter(record)
    assert "patient_jane" not in record.getMessage()


def test_cli_unexpected_errors_never_echo_database_parameters(monkeypatch, capsys):
    from claimlens.cli import main
    from claimlens.storage import db
    def broken(url):
        raise RuntimeError("database password=private-secret Patient Jane Example")
    monkeypatch.setattr(db, "make_engine", broken)
    assert main(["db-init"]) == 2
    captured = capsys.readouterr()
    assert "private-secret" not in captured.err and "Jane" not in captured.err


def test_pipeline_never_logs_the_original_filename(tmp_path, caplog):
    path = tmp_path / "Jane_Example_123-45-6789.txt"
    path.write_text("Provider Appeal of payment determination\nPayer: Aetna\n")
    with caplog.at_level(logging.INFO, logger="claimlens.pipeline"):
        process_file(path, MockLLM())
    assert "Jane" not in " ".join(r.getMessage() for r in caplog.records)


def test_validation_responses_do_not_echo_sensitive_inputs(make_api):
    client, _ = make_api()
    response = client.post("/uploads", headers=H["upload"], json={"filename": {"ssn": "123-45-6789"}})
    assert response.status_code == 422
    assert "123-45" not in response.text and "input" not in response.text
    doc = upload_incomplete(client)
    response = client.post(f"/documents/{doc['id']}/review", headers=H["review"],
                           json={"decision": "123-45-6789", "expected_version": 1})
    assert response.status_code == 422 and "123-45" not in response.text


def test_opaque_names_do_not_disable_content_deduplication(make_api):
    client, service = make_api()
    data = b"Provider Appeal of payment determination\nPayer: Aetna\n"
    first = upload(client, "Jane_Example.txt", data)
    second = upload(client, "Different_Patient.txt", data)
    assert first.status_code == 201 and second.status_code == 200
    assert first.json()["document"]["id"] == second.json()["document"]["id"]
    assert "Jane" not in first.text and "Different" not in second.text


def test_legacy_filename_mode_is_explicit_opt_in(make_api):
    from claimlens.config import get_settings
    client, _ = make_api(settings=dataclasses.replace(get_settings(), opaque_source_names=False))
    assert upload(client, "../demo.txt", b"appeal").json()["document"]["source_name"] == "demo.txt"
