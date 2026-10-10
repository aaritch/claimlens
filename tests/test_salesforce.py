import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError

import pytest
from sqlalchemy import select

from claimlens.integrations.salesforce import SalesforceClient, SalesforceError, _NoRedirect, sync_document
from claimlens.storage import repository as repo
from claimlens.storage.db import AuditLog
from tests.conftest import make_result


def client():
    return SalesforceClient("https://demo.my.salesforce.com", "test-token")

def test_client_repr_does_not_expose_access_token():
    assert "test-token" not in repr(client())


def document(session, status="validated"):
    return repo.save_result(session, make_result(status), content_sha256="a" * 64, fingerprint="fp", source_name="private.pdf")


@pytest.mark.parametrize("url", [
    "http://demo.my.salesforce.com", "https://evil.example", "https://demo.my.salesforce.com.evil.example",
    "https://token@demo.my.salesforce.com", "https://demo.my.salesforce.com/path",
    "https://demo.my.salesforce.com:8080", "https://demo.my.salesforce.com?token=secret",
])
def test_salesforce_never_sends_data_to_unapproved_hosts(url):
    with pytest.raises(ValueError):
        SalesforceClient(url, "token")


def test_dry_run_is_network_free_and_contains_no_field_values(session_factory):
    adapter = Mock()
    with session_factory() as session:
        doc = document(session)
        preview = sync_document(session, doc.id, adapter)
        assert preview["mode"] == "dry_run"
        assert "1450" not in json.dumps(preview) and "Riverbend" not in json.dumps(preview)
        assert session.scalars(select(AuditLog)).all() == []
    adapter.upsert.assert_not_called()


def test_only_validated_documents_can_be_exported(session_factory):
    with session_factory() as session:
        doc = document(session, "needs_review")
        with pytest.raises(ValueError):
            sync_document(session, doc.id, client(), execute=True)


def test_upsert_uses_document_uuid_as_external_id_and_allowlisted_fields(session_factory):
    opener = MagicMock()
    opener.open.return_value.__enter__.return_value = SimpleNamespace(status=204)
    with session_factory() as session:
        doc = document(session)
        client().upsert(doc, opener=opener)
        request = opener.open.call_args.args[0]
        assert request.method == "PATCH" and request.full_url.endswith(f"/Document_ID__c/{doc.id}")
        payload = json.loads(request.data)
        assert payload["Disputed_Amount__c"] == 1450.5 and payload["Service_Date__c"] == "2024-03-04"
        assert "summary" not in payload and "provider_name" not in payload
        assert opener.open.call_args.kwargs["timeout"] == 15


@pytest.mark.parametrize("status,retryable", [(401, False), (429, True), (500, True), (302, False)])
def test_remote_errors_never_echo_tokens_or_response_bodies(session_factory, status, retryable):
    opener = Mock()
    opener.open.side_effect = HTTPError("https://host", status, "SECRET-TOKEN PRIVATE-BODY", {}, None)
    with session_factory() as session:
        with pytest.raises(SalesforceError) as exc:
            client().upsert(document(session), opener=opener)
    assert exc.value.retryable == retryable and "SECRET" not in str(exc.value)


def test_redirects_are_not_followed():
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example") is None


def test_executed_exports_are_audited(session_factory):
    adapter = Mock()
    with session_factory() as session:
        doc = document(session)
        assert sync_document(session, doc.id, adapter, execute=True)["mode"] == "executed"
        session.commit()
        assert session.scalars(select(AuditLog)).one().action == "export_salesforce"
    adapter.upsert.assert_called_once()


def test_cli_requires_synthetic_acknowledgement_before_creating_client(monkeypatch):
    from claimlens.cli import main
    monkeypatch.delenv("CLAIMLENS_SALESFORCE_TOKEN", raising=False)
    assert main(["sync-salesforce", "--document-id", "x", "--execute"]) == 2
