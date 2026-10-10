"""Opt-in real-browser tests against an isolated localhost API and synthetic-only SQLite database."""
import dataclasses
import os
import socket
import threading
import time

import pytest
import uvicorn

from claimlens.api.app import create_app
from claimlens.api.auth import Authenticator
from claimlens.config import get_settings
from claimlens.llm.mock import MockLLM
from claimlens.service import ProcessingService
from claimlens.storage import repository as repo
from claimlens.storage.idempotency import SqlIdempotencyStore
from tests.conftest import make_result

pytestmark = pytest.mark.skipif(os.getenv("CLAIMLENS_BROWSER_TESTS") != "1", reason="opt-in browser tests")


@pytest.fixture
def browser_page(session_factory):
    from playwright.sync_api import sync_playwright
    settings = dataclasses.replace(get_settings(), metrics_enabled=False)
    service = ProcessingService(session_factory, MockLLM(), SqlIdempotencyStore(session_factory), settings=settings)
    app = create_app(service, session_factory, settings, Authenticator(disabled=True))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_config=None))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.getenv("CLAIMLENS_BROWSER_CHANNEL") or None)
            context = browser.new_context()
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(f"http://127.0.0.1:{port}/review/")
                yield page
                assert errors == []
            finally:
                context.close()
                browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


def seed(session_factory, n=0, **claim):
    with session_factory() as session:
        doc = repo.save_result(session, make_result(**claim), content_sha256=str(n) * 64, fingerprint="fp",
                               source_name="opaque.pdf")
        session.commit()
        return doc.id


def select(page, document_id):
    from playwright.sync_api import expect
    page.locator("#documents button").filter(has_text=document_id[:8]).click()
    expect(page.locator("#detail-title")).to_have_text(f"Document {document_id[:8]}")


def test_browser_review_corrections_invalid_data_rejection_and_disconnect(browser_page, session_factory):
    from playwright.sync_api import expect
    first = seed(session_factory, provider_npi=None)
    second = seed(session_factory, 1)
    page = browser_page
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#documents button")).to_have_count(2)
    select(page, first)
    page.locator("#field-provider_npi").fill("9999999999")
    page.get_by_role("button", name="Approve with corrections").click()
    expect(page.locator("#notice")).to_contain_text("failed validation")
    page.locator("#field-provider_npi").fill("1234567893")
    page.get_by_role("button", name="Approve with corrections").click()
    expect(page.locator("#notice")).to_have_text("Decision saved: approve.")
    expect(page.locator("#documents button")).to_have_count(1)
    select(page, second)
    page.locator("#note").fill("Synthetic rejection for review workflow")
    page.get_by_role("button", name="Reject", exact=True).click()
    expect(page.locator("#notice")).to_have_text("Decision saved: reject.")
    expect(page.locator("#documents button")).to_have_count(0)
    page.get_by_role("button", name="Disconnect", exact=True).click()
    expect(page.locator("#review-form")).to_be_hidden()
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_browser_stale_versions_are_not_silently_overwritten(browser_page, session_factory):
    from playwright.sync_api import expect
    identifier = seed(session_factory)
    page = browser_page
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#documents button")).to_have_count(1)
    select(page, identifier)
    with session_factory() as session:
        repo.apply_review(session, identifier, "other-reviewer", "reject", get_settings(),
                          expected_version=1, note="Concurrent synthetic decision")
        session.commit()
    page.get_by_role("button", name="Approve with corrections").click()
    expect(page.locator("#notice")).to_contain_text("changed")
    with session_factory() as session:
        assert repo.get_document(session, identifier).status == "rejected"
