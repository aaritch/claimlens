"""The env-driven wiring (what `serve` actually runs) and the Phase 4 CLI commands."""
import json

import pytest
from fastapi.testclient import TestClient

from claimlens.api.auth import AuthConfigError
from claimlens.api.main import build_app_from_env
from claimlens.cli import main
from claimlens.eval.runner import load_ground_truth


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("CLAIMLENS_API_TOKENS", "CLAIMLENS_API_AUTH", "CLAIMLENS_REDIS_URL", "CLAIMLENS_IDEMPOTENCY",
              "CLAIMLENS_MODEL_ID", "CLAIMLENS_OCR", "CLAIMLENS_API_DOCS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLAIMLENS_DATABASE_URL", f"sqlite:///{tmp_path / 'wiring.db'}")
    monkeypatch.setenv("CLAIMLENS_LLM", "mock:careful")
    return monkeypatch


def test_server_refuses_to_start_without_auth_configuration(env):
    with pytest.raises(AuthConfigError, match="no API tokens"):
        build_app_from_env()


def test_env_built_app_serves_requests(env, samples):
    assert main(["db-init"]) == 0
    env.setenv("CLAIMLENS_API_TOKENS", "admin-token-0123456789:ada:admin")
    client = TestClient(build_app_from_env())
    f = next(samples.glob("01_*"))
    h = {"Authorization": "Bearer admin-token-0123456789"}
    assert client.post("/documents", files={"file": (f.name, f.read_bytes())}, headers=h).status_code == 201
    assert client.post("/documents", files={"file": (f.name, f.read_bytes())}, headers=h).status_code == 200
    assert client.get("/documents", headers=h).json()["items"][0]["status"] == "validated"


def test_docs_can_be_switched_off_by_environment(env):
    env.setenv("CLAIMLENS_API_AUTH", "disabled")
    env.setenv("CLAIMLENS_API_DOCS", "0")
    assert TestClient(build_app_from_env()).get("/docs").status_code == 404


def test_redis_url_enables_the_fast_path(env):
    import fakeredis
    from claimlens.storage import cache as cache_mod
    env.setenv("CLAIMLENS_API_AUTH", "disabled")
    env.setenv("CLAIMLENS_REDIS_URL", "redis://example:6379/0")
    env.setattr(cache_mod.redis.Redis, "from_url", classmethod(lambda cls, url, **kw: fakeredis.FakeRedis()))
    from claimlens.api.main import build_service
    service, _, _ = build_service()
    assert isinstance(service.cache, cache_mod.RedisCache)


def test_export_ground_truth_command(env, samples, tmp_path, capsys):
    from claimlens.storage.db import make_engine, make_session_factory
    from claimlens.config import get_settings
    assert main(["db-init"]) == 0
    env.setenv("CLAIMLENS_API_AUTH", "disabled")
    client = TestClient(build_app_from_env())
    r = client.post("/documents", files={"file": ("a.txt", b"Provider Appeal of payment determination\nPayer: Aetna\nx\n")})
    doc = r.json()["document"]
    fix = {"provider_npi": "1234567893", "disputed_amount": 99.5, "claim_reference": "CLM-424242"}
    assert client.post(f"/documents/{doc['id']}/review", json={"decision": "approve", "expected_version": 1,
                                                               "corrections": fix}).status_code == 200
    out = tmp_path / "reviewed"
    assert main(["export-ground-truth", "--out", str(out)]) == 0
    assert "Wrote 1 answer key" in capsys.readouterr().out
    assert load_ground_truth(out)[0]["expected"]["claim_reference"] == "CLM-424242"
