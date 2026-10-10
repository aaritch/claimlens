"""Production-image smoke test using runtime dependencies only, no cloud/network access or host data."""
from __future__ import annotations

import io
import json
import os
import socket
import threading
import time
from urllib.request import Request, urlopen

import uvicorn
from PIL import Image, ImageDraw, ImageFont

from claimlens.api.app import create_app
from claimlens.api.auth import Authenticator
from claimlens.api.main import build_service
from claimlens.integrations.salesforce import sync_document
from claimlens.logging_safe import configure_logging
from claimlens.ocr.tesseract import TesseractEngine
from claimlens.storage.migrations import migrate


def main() -> None:
    configure_logging()
    assert os.getuid() == 10001, "the image must run as its non-root user"
    service, sessions, settings = build_service()
    assert settings.database_url.startswith("sqlite:////tmp/"), "use an isolated temporary database"
    engine = sessions.kw["bind"]
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = None
    thread = None
    try:
        migrate(engine)
        migrate(engine)
        app = create_app(service, sessions, settings, Authenticator(disabled=True))
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_config=None))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started, "API startup failed"
        for path in ("/healthz", "/readyz", "/review/", "/review/app.js", "/review/style.css"):
            with urlopen(base + path, timeout=5) as response:
                assert response.status == 200
        boundary = "claimlens-synthetic-smoke"
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"Synthetic_Name.txt\"\r\n"
            "Content-Type: text/plain\r\n\r\nProvider Appeal of payment determination\nPayer: Aetna\n"
            f"\r\n--{boundary}--\r\n"
        ).encode()
        def upload():
            request = Request(base + "/documents", data=body,
                              headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        status, first = upload()
        duplicate_status, duplicate = upload()
        assert status == 201 and duplicate_status == 200
        document = first["document"]
        assert document["id"] == duplicate["document"]["id"]
        assert "Synthetic_Name" not in document["source_name"]
        request = Request(base + f"/documents/{document['id']}/review", method="POST", data=json.dumps({
            "decision": "approve", "expected_version": document["version"],
            "corrections": {"provider_npi": "1234567893", "disputed_amount": 20, "claim_reference": "SMOKE-1"},
        }).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            assert json.load(response)["status"] == "validated"
        with sessions() as session:
            assert sync_document(session, document["id"])["mode"] == "dry_run"
        image = Image.new("RGB", (900, 130), "white")
        ImageDraw.Draw(image).text((20, 35), "SYNTHETIC DOCUMENT", fill="black", font=ImageFont.load_default(size=40))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        ocr = TesseractEngine().extract(buffer.getvalue(), "synthetic.png")
        assert ocr.pages == 1 and "SYNTHETIC" in ocr.text.upper(), "local OCR failed"
        print("Container smoke passed: non-root, migrations, API, UI assets, deduplication, review, dry run, real OCR.")
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        listener.close()
        engine.dispose()


if __name__ == "__main__":
    main()
