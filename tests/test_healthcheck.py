import http.server
import os
import threading
import time

import pytest

import claimlens.worker as worker_mod
from claimlens.cli import main


@pytest.fixture
def liveness(tmp_path, monkeypatch):
    f = tmp_path / "alive"
    monkeypatch.setattr(worker_mod, "LIVENESS_FILE", f)
    monkeypatch.setenv("CLAIMLENS_API_PORT", "1")   # nothing listens on port 1: API probe fails unless a test starts one
    return f


def test_fresh_worker_heartbeat_is_healthy(liveness):
    liveness.touch()
    assert main(["healthcheck"]) == 0


def test_stale_worker_heartbeat_is_unhealthy(liveness):
    liveness.touch()
    old = time.time() - 600
    os.utime(liveness, (old, old))
    assert main(["healthcheck"]) == 1


def test_no_heartbeat_and_no_api_is_unhealthy(liveness):
    assert main(["healthcheck"]) == 1


def test_answering_api_is_healthy(liveness, monkeypatch):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/healthz" else 404)
            self.end_headers()

        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("CLAIMLENS_API_PORT", str(server.server_port))
    try:
        assert main(["healthcheck"]) == 0
    finally:
        server.shutdown()
