"""Reproducible offline demo. Creates only a NEW output directory and an isolated temporary database."""
from __future__ import annotations

import argparse
import dataclasses
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from claimlens.analytics import export_parquet
from claimlens.api.app import create_app
from claimlens.api.auth import Authenticator
from claimlens.config import get_settings
from claimlens.datagen import generate_samples
from claimlens.eval.report import render_report
from claimlens.eval.runner import RunConfig, evaluate
from claimlens.integrations.salesforce import sync_document
from claimlens.llm.mock import MockLLM
from claimlens.service import ProcessingService
from claimlens.storage.db import make_engine, make_session_factory
from claimlens.storage.idempotency import SqlIdempotencyStore
from claimlens.storage.migrations import migrate


def demo(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=False)
    samples = out / "samples"
    generate_samples(samples, count=12, seed=7)
    report = evaluate(samples, RunConfig(label="demo", llm="mock:careful"), MockLLM("careful"))
    (out / "evaluation.md").write_text(render_report(report), encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="claimlens-demo-") as temporary:
        settings = dataclasses.replace(get_settings(), database_url=f"sqlite:///{Path(temporary) / 'demo.db'}",
                                       metrics_enabled=False, opaque_source_names=True)
        engine = make_engine(settings.database_url)
        try:
            migrate(engine)
            sessions = make_session_factory(engine)
            service = ProcessingService(sessions, MockLLM("careful"), SqlIdempotencyStore(sessions), settings=settings)
            app = create_app(service, sessions, settings, Authenticator(disabled=True))
            with TestClient(app) as client:
                path = next(samples.glob("01_*"))
                first = client.post("/documents", files={"file": (path.name, path.read_bytes())})
                duplicate = client.post("/documents", files={"file": (path.name, path.read_bytes())})
                assert first.status_code == 201 and duplicate.status_code == 200
                claim_id = first.json()["document"]["id"]
                incomplete = client.post("/documents", files={"file": (
                    "review.txt", b"Provider Appeal of payment determination\nPayer: Aetna\n",
                )}).json()["document"]
                reviewed = client.post(f"/documents/{incomplete['id']}/review", json={
                    "decision": "approve", "expected_version": incomplete["version"],
                    "corrections": {"provider_npi": "1234567893", "disputed_amount": 20, "claim_reference": "DEMO-1"},
                })
                assert reviewed.status_code == 200
                with sessions() as session:
                    count = export_parquet(session, out / "claims.parquet")
                    preview = sync_document(session, claim_id)
                    session.commit()
            (out / "summary.json").write_text(json.dumps({
                "mode": "offline_synthetic", "upload": first.status_code,
                "duplicate": duplicate.status_code, "review": reviewed.status_code,
                "exported": count, "salesforce": preview["mode"], "model_f1": report.micro.f1,
            }, indent=2), encoding="utf-8")
        finally:
            engine.dispose()
    print("Offline demo complete: upload, duplicate protection, review, evaluation, Parquet, Salesforce dry run.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("results/demo"))
    demo(parser.parse_args().out)
