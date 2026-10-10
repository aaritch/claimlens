from pathlib import Path
from tests.test_api import make_api


def test_review_ui_and_assets_are_served_without_exposing_claims(make_api):
    client, _ = make_api(enable_docs=False)
    response = client.get("/review/")
    assert response.status_code == 200 and "Review desk" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert client.get("/review/app.js").status_code == 200
    assert client.get("/review/style.css").status_code == 200
    assert client.get("/claims").status_code == 401
    assert client.get("/review/../auth.py").status_code == 404


def test_review_ui_uses_safe_dom_and_never_persists_bearer_tokens():
    script = (Path(__file__).resolve().parents[1] / "claimlens/api/static/app.js").read_text()
    assert not any(unsafe in script for unsafe in ("innerHTML", "localStorage", "sessionStorage", "document.cookie", "eval("))
    assert "expected_version: current.version" in script and "encodeURIComponent" in script
    assert 'cache: "no-store"' in script
