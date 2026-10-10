import json
from scripts.demo import demo


def test_demo_is_offline_and_does_not_touch_the_users_database(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAIMLENS_DATABASE_URL", "postgresql://do-not-connect.invalid/user")
    out = tmp_path / "demo"
    demo(out)
    report = json.loads((out / "summary.json").read_text())
    assert report["upload"] == 201 and report["duplicate"] == 200 and report["review"] == 200
    assert report["exported"] == 2 and report["salesforce"] == "dry_run"
    assert (out / "claims.parquet").is_file() and (out / "evaluation.md").is_file()
