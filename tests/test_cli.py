import json

from claimlens.cli import main


def test_gen_data_then_run_dir_with_mock(tmp_path, capsys):
    samples, results = tmp_path / "s", tmp_path / "r"
    assert main(["gen-data", "--out", str(samples), "--count", "6"]) == 0
    assert main(["run-dir", str(samples), "--mock", "--out", str(results)]) == 0
    out = capsys.readouterr().out
    assert "validated=6" in out and "needs_ocr=1" in out
    saved = json.loads(next(results.glob("01_*.json")).read_text())
    assert saved["status"] == "validated" and saved["claim"]["provider_npi"]


def test_missing_model_config_exits_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CLAIMLENS_MODEL_ID", raising=False)
    main(["gen-data", "--out", str(tmp_path), "--count", "3"])
    code = main(["run", str(next(tmp_path.glob("01_*"))), ])
    assert code == 2 and "CLAIMLENS_MODEL_ID" in capsys.readouterr().err
