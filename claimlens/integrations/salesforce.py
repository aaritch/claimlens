"""Synthetic-data Salesforce upserts. Dry run is the default; URLs, object names and external IDs are constrained."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from claimlens.storage import repository as repo

FIELDS = {
    "Document_Type__c": "doc_type", "Provider_NPI__c": "provider_npi",
    "Payer_Name__c": "payer_name", "Service_Date__c": "service_date",
    "Disputed_Amount__c": "disputed_amount",
}


class SalesforceError(RuntimeError):
    def __init__(self, retryable: bool = False):
        super().__init__("Salesforce request failed")
        self.retryable = retryable


class _NoRedirect(HTTPRedirectHandler):
    # Never forward an Authorization header or claim payload to a redirected host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class SalesforceClient:
    instance_url: str
    access_token: str = field(repr=False)
    object_name: str = "Claim__c"
    external_id_field: str = "Document_ID__c"
    api_version: str = "v61.0"

    def __post_init__(self):
        parsed = urlsplit(self.instance_url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or not host.endswith(".my.salesforce.com")
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("use the HTTPS My Domain URL of your Salesforce org")
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*__c", value)
               for value in (self.object_name, self.external_id_field)):
            raise ValueError("Salesforce object and external ID must be custom API names")
        if not re.fullmatch(r"v[0-9]{2,3}\.0", self.api_version) or not self.access_token:
            raise ValueError("Salesforce token and valid API version required")
        self.instance_url = self.instance_url.rstrip("/")

    def upsert(self, document, opener=None) -> None:
        if document.status != "validated":
            raise ValueError("only validated documents may be exported")
        path = (f"/services/data/{self.api_version}/sobjects/{self.object_name}/"
                f"{self.external_id_field}/{quote(document.id, safe='')}")
        payload = {external: getattr(document, internal) for external, internal in FIELDS.items()}
        if payload["Service_Date__c"]:
            payload["Service_Date__c"] = payload["Service_Date__c"].isoformat()
        request = Request(self.instance_url + path, data=json.dumps(payload).encode(), method="PATCH",
                          headers={"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"})
        try:
            with (opener or build_opener(_NoRedirect())).open(request, timeout=15) as response:
                if response.status not in (201, 204):
                    raise SalesforceError(response.status == 429 or response.status >= 500)
        except HTTPError as exc:
            raise SalesforceError(exc.code == 429 or exc.code >= 500) from None
        except (URLError, TimeoutError, OSError):
            raise SalesforceError(True) from None


def sync_document(session, document_id: str, client: SalesforceClient | None = None, *,
                  execute: bool = False, actor: str = "salesforce_export") -> dict:
    document = repo.get_document(session, document_id)
    if document is None or document.status != "validated":
        raise ValueError("select an existing validated document")
    if execute:
        if client is None:
            raise ValueError("Salesforce client required for execution")
        client.upsert(document)
        repo.audit(session, actor, "export_salesforce", document.id)
    # Preview never prints values or credentials.
    return {"document_id": document.id, "mode": "executed" if execute else "dry_run",
            "fields": sorted(FIELDS)}
