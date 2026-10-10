"""Command-line interface.

  python -m claimlens gen-data --out data/samples --count 12
  python -m claimlens run data/samples/01_surprise_billing_complaint.docx --mock
  python -m claimlens run-dir data/samples --mock --out results
  python -m claimlens eval --data data/hard --config "label=base,llm=mock:careful"
  python -m claimlens compare --data data/hard --config "label=A,llm=mock:naive" --config "label=B,llm=mock:careful"
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from claimlens.config import get_settings
from claimlens.eval.report import render_compare, render_report
from claimlens.eval.runner import build_llms, build_ocr_for, evaluate, parse_config
from claimlens.ocr.base import OcrError
from claimlens.ocr.factory import build_ocr
from claimlens.llm.bedrock_client import BedrockClient, LLMError
from claimlens.llm.mock import MockLLM
from claimlens.pipeline import process_file
from claimlens.logging_safe import configure_logging
from claimlens.storage.migrations import MigrationError

SUPPORTED = {".pdf", ".docx", ".eml", ".txt", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def _build_llms(use_mock: bool):
    if use_mock:
        mock = MockLLM()
        return mock, mock
    s = get_settings()
    return (BedrockClient(s.model_id, s.region), BedrockClient(s.classify_model_id, s.region))


def _run_paths(paths: list[Path], use_mock: bool, out: Path | None, ocr_spec: str = "none") -> int:
    llm, classify_llm = _build_llms(use_mock)
    ocr = build_ocr(ocr_spec, get_settings(), oracle_dir=paths[0].parent / "ocr_oracle" if paths else None)
    if out:
        out.mkdir(parents=True, exist_ok=True)
    total_cost, counts = 0.0, {}
    print(f"{'FILE':<48}{'STATUS':<14}{'TOKENS(in/out)':<18}{'COST':<10}MS")
    for p in paths:
        r = process_file(p, llm, classify_llm=classify_llm, ocr=ocr)
        total_cost += r.usage.cost_usd
        counts[r.status] = counts.get(r.status, 0) + 1
        ocr_note = f"  [OCR {'+'.join(r.ocr.engines)} p={r.ocr.pages}]" if r.ocr else ""
        print(f"{r.filename:<48}{r.status:<14}{r.usage.input_tokens}/{r.usage.output_tokens:<12}"
              f"${r.usage.cost_usd:<9.5f}{r.elapsed_ms}{ocr_note}")
        if r.error:
            print(f"    error: {r.error}")
        if out:
            (out / f"{p.stem}.result.json").write_text(r.model_dump_json(indent=2))
    print(f"\n{len(paths)} files | " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
          + f" | total cost ${total_cost:.5f}")
    return 0


def _evaluate_config(spec: str, args):
    cfg = parse_config(spec)
    settings = get_settings()
    llm, classify_llm = build_llms(cfg, settings)
    ocr = build_ocr_for(cfg, settings, args.data)
    return evaluate(args.data, cfg, llm, classify_llm, settings, workers=args.workers,
                    show_values=getattr(args, "show_values", False), ocr=ocr)


def _check_gates(report, args) -> list[str]:
    failures = []
    if args.min_f1 is not None and (report.micro.f1 or 0) < args.min_f1:
        failures.append(f"micro F1 {report.micro.f1:.3f} < {args.min_f1}")
    if args.min_auto_accept_precision is not None and (report.auto_accept_precision or 0) < args.min_auto_accept_precision:
        failures.append(f"auto-accept precision {report.auto_accept_precision} < {args.min_auto_accept_precision}")
    if args.min_error_catch_rate is not None and (report.error_catch_rate if report.error_catch_rate is not None else 1.0) < args.min_error_catch_rate:
        failures.append(f"error catch rate {report.error_catch_rate} < {args.min_error_catch_rate}")
    return failures


def _healthy() -> bool:
    """Used by the container HEALTHCHECK. A stuck worker stops touching its heartbeat file; a dead API stops answering."""
    import time
    import urllib.request
    from claimlens import worker
    try:
        if time.time() - worker.LIVENESS_FILE.stat().st_mtime < 120:
            return True
    except OSError:
        pass
    try:
        port = os.getenv("CLAIMLENS_API_PORT", "8080")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as r:  # noqa: S310 (fixed localhost URL)
            return r.status == 200
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claimlens")
    sub = parser.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen-data", help="generate synthetic documents + answer keys")
    g.add_argument("--out", type=Path, default=Path("data/samples"))
    g.add_argument("--count", type=int, default=12)
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--difficulty", choices=["clean", "hard"], default="clean",
                   help="'hard' injects typo'd NPIs, distractor amounts and missing dates")
    g.add_argument("--scan-fraction", type=float, default=0.0,
                   help="fraction of documents rendered as noisy scans (0.3 = 30%%) to exercise OCR")

    r = sub.add_parser("run", help="process one file")
    r.add_argument("path", type=Path)
    r.add_argument("--mock", action="store_true", help="use the offline mock instead of Bedrock")
    r.add_argument("--out", type=Path, default=None, help="folder to save result JSON")
    r.add_argument("--ocr", default="none", help="none | tesseract | textract | tesseract+textract | oracle[:rate]")

    d = sub.add_parser("run-dir", help="process every supported file in a folder")
    d.add_argument("path", type=Path)
    d.add_argument("--mock", action="store_true")
    d.add_argument("--out", type=Path, default=Path("results"))
    d.add_argument("--ocr", default="none", help="none | tesseract | textract | tesseract+textract | oracle[:rate]")

    def add_eval_args(sp, many_configs: bool):
        sp.add_argument("--data", type=Path, required=True, help="folder made by gen-data (needs ground_truth/)")
        sp.add_argument("--config", action="append" if many_configs else None, required=many_configs,
                        default=None if many_configs else "label=default,llm=bedrock",
                        help='e.g. "label=v2,llm=bedrock,extract_prompt=extract_v2,model_id=<id>" or "llm=mock:naive"')
        sp.add_argument("--workers", type=int, default=4)
        sp.add_argument("--out", type=Path, default=Path("results/eval"))

    e = sub.add_parser("eval", help="score the pipeline against ground truth")
    add_eval_args(e, many_configs=False)
    e.add_argument("--show-values", action="store_true", help="include expected/got values (synthetic data only!)")
    e.add_argument("--min-f1", type=float, default=None, help="exit 1 if micro F1 is below this")
    e.add_argument("--min-auto-accept-precision", type=float, default=None)
    e.add_argument("--min-error-catch-rate", type=float, default=None)

    sv = sub.add_parser("serve", help="run the REST API (needs CLAIMLENS_API_TOKENS or CLAIMLENS_API_AUTH=disabled)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8080)

    sub.add_parser("healthcheck", help="exit 0 if this container is healthy (worker heartbeat fresh OR API answering)")
    sub.add_parser("worker", help="run the SQS worker (this is the process that runs on Fargate)")

    sub.add_parser("db-init", help="create database tables (and the DynamoDB table if CLAIMLENS_IDEMPOTENCY=dynamodb)")
    dm = sub.add_parser("db-migrate", help="upgrade the database using versioned migrations")
    dm.add_argument("--adopt-legacy", action="store_true", help="stamp a schema-checked legacy database (back it up first)")

    eg = sub.add_parser("export-ground-truth", help="turn human-approved reviews into evaluation answer keys")
    eg.add_argument("--out", type=Path, required=True)

    ae = sub.add_parser("export-analytics", help="export validated business fields as Parquet (optional analytics dependencies)")
    ae.add_argument("--out", type=Path, required=True)
    ae.add_argument("--upload-bucket", default="", help="explicitly upload the new export to S3")
    ae.add_argument("--key", default="", help="S3 key under exports/, ending in .parquet")
    ae.add_argument("--kms-key", default="", help="customer-managed key for the uploaded export")
    sf = sub.add_parser("sync-salesforce", help="preview a validated claim upsert (synthetic data only)")
    sf.add_argument("--document-id", required=True)
    sf.add_argument("--execute", action="store_true", help="actually send business fields to the configured Salesforce org")
    sf.add_argument("--synthetic-data-only", action="store_true", help="acknowledge that the selected claim is synthetic")

    c = sub.add_parser("compare", help="A/B two configurations on the same documents")
    add_eval_args(c, many_configs=True)

    args = parser.parse_args(argv)
    configure_logging()

    try:
        if args.cmd == "gen-data":
            from claimlens.datagen import generate_samples  # lazy: dev-only dependency (reportlab)
            files = generate_samples(args.out, args.count, args.seed, difficulty=args.difficulty,
                                     scan_fraction=args.scan_fraction)
            print(f"Wrote {len(files)} files to {args.out} (answer keys in {args.out / 'ground_truth'})")
            return 0
        if args.cmd == "serve":
            import uvicorn
            uvicorn.run("claimlens.api.main:build_app_from_env", factory=True, host=args.host, port=args.port,
                        access_log=False, log_config=None)
            return 0
        if args.cmd == "healthcheck":
            return 0 if _healthy() else 1
        if args.cmd == "worker":
            import boto3
            from claimlens.api.main import build_service
            from claimlens.worker import Worker
            logging.getLogger().setLevel(logging.INFO)
            service, session_factory, cfg = build_service()
            if not cfg.sqs_queue_url:
                print("CLAIMLENS_SQS_QUEUE_URL is not set", file=sys.stderr)
                return 2
            Worker(service, boto3.client("sqs", region_name=cfg.region), boto3.client("s3", region_name=cfg.region),
                   session_factory, cfg).run_forever()
            return 0
        if args.cmd == "db-init":
            from claimlens.storage.db import init_db, make_engine
            from claimlens.storage.idempotency import create_dynamodb_table
            cfg = get_settings()
            init_db(make_engine(cfg.database_url))
            msg = "database tables ready"
            if cfg.idempotency_backend == "dynamodb":
                create_dynamodb_table(cfg.dynamodb_table, cfg.region, cfg.dynamodb_endpoint)
                msg += f"; DynamoDB table '{cfg.dynamodb_table}' ready"
            print(msg)
            return 0
        if args.cmd == "db-migrate":
            from claimlens.storage.db import make_engine
            from claimlens.storage.migrations import migrate
            engine = make_engine(get_settings().database_url)
            try:
                migrate(engine, adopt_legacy=args.adopt_legacy)
            finally:
                engine.dispose()
            print("database migrations complete")
            return 0
        if args.cmd == "export-ground-truth":
            from claimlens.storage import repository as repo
            from claimlens.storage.db import make_engine, make_session_factory
            with make_session_factory(make_engine(get_settings().database_url))() as session:
                n = repo.export_ground_truth(session, args.out)
            print(f"Wrote {n} answer key(s) to {args.out / 'ground_truth'} (copy the original files next to them to evaluate)")
            return 0
        if args.cmd == "export-analytics":
            from claimlens.analytics import export_parquet, upload_export
            from claimlens.storage.db import make_engine, make_session_factory
            if args.upload_bucket and (not args.kms_key or not args.key.startswith("exports/") or not args.key.endswith(".parquet")):
                raise ValueError("provide the KMS key and exports/*.parquet S3 destination")
            if not args.upload_bucket and (args.key or args.kms_key):
                raise ValueError("S3 options require an upload bucket")
            engine = make_engine(get_settings().database_url)
            try:
                with make_session_factory(engine)() as session:
                    n = export_parquet(session, args.out)
                    session.commit()
                if args.upload_bucket:
                    import boto3
                    upload_export(args.out, args.upload_bucket, args.key, args.kms_key,
                                  boto3.client("s3", region_name=get_settings().region))
            finally:
                engine.dispose()
            print(f"Exported {n} validated record(s)")
            return 0
        if args.cmd == "sync-salesforce":
            import json
            from claimlens.integrations.salesforce import SalesforceClient, SalesforceError, sync_document
            from claimlens.storage.db import make_engine, make_session_factory
            if args.execute and not args.synthetic_data_only:
                raise ValueError("execution requires synthetic-data acknowledgement")
            client = (SalesforceClient(
                os.getenv("CLAIMLENS_SALESFORCE_URL", ""), os.getenv("CLAIMLENS_SALESFORCE_TOKEN", ""),
                os.getenv("CLAIMLENS_SALESFORCE_OBJECT", "Claim__c"),
                os.getenv("CLAIMLENS_SALESFORCE_EXTERNAL_ID", "Document_ID__c"),
                os.getenv("CLAIMLENS_SALESFORCE_API_VERSION", "v61.0"),
            ) if args.execute else None)
            engine = make_engine(get_settings().database_url)
            try:
                with make_session_factory(engine)() as session:
                    preview = sync_document(session, args.document_id, client, execute=args.execute)
                    session.commit()
            except SalesforceError as exc:
                print(f"Salesforce export failed; retryable={exc.retryable}", file=sys.stderr)
                return 1
            finally:
                engine.dispose()
            print(json.dumps(preview))
            return 0
        if args.cmd == "eval":
            report = _evaluate_config(args.config, args)
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / f"{report.label}.eval.json").write_text(report.model_dump_json(indent=2))
            text = render_report(report)
            (args.out / f"{report.label}.eval.md").write_text(text)
            print(text)
            failures = _check_gates(report, args)
            for f in failures:
                print(f"GATE FAILED: {f}", file=sys.stderr)
            return 1 if failures else 0
        if args.cmd == "compare":
            if len(args.config) != 2:
                parser.error("compare needs exactly two --config options (A then B)")
            ra, rb = (_evaluate_config(spec, args) for spec in args.config)
            args.out.mkdir(parents=True, exist_ok=True)
            text = render_compare(ra, rb)
            (args.out / f"compare_{ra.label}_vs_{rb.label}.md").write_text(text)
            for rep in (ra, rb):
                (args.out / f"{rep.label}.eval.json").write_text(rep.model_dump_json(indent=2))
            print(text)
            return 0
        if args.cmd == "run":
            return _run_paths([args.path], args.mock, args.out, args.ocr)
        files = sorted(p for p in args.path.iterdir() if p.suffix.lower() in SUPPORTED)
        return _run_paths(files, args.mock, args.out, args.ocr)
    except LLMError as exc:
        print(f"LLM configuration/runtime error: {exc}", file=sys.stderr)
        return 2
    except OcrError as exc:
        print(f"OCR configuration error: {exc}", file=sys.stderr)
        return 2
    except MigrationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError:
        print("Invalid configuration or input. Check the command and documented settings.", file=sys.stderr)
        return 2
    except FileExistsError:
        print("Output already exists. Choose a new export path.", file=sys.stderr)
        return 2
    except ModuleNotFoundError:
        print("A required dependency is missing. Install the documented requirements for this command.", file=sys.stderr)
        return 2
    except Exception as exc:
        logging.getLogger(__name__).error("command failed", extra={
            "event": "command_failed", "fields": {"error_class": exc.__class__.__name__},
        })
        print("Command failed. Check configuration and the safe operational logs.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
