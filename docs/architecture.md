# ClaimLens architecture

ClaimLens is a synthetic-data document-intake reference implementation. It does not certify HIPAA compliance.

```text
Local CLI -> parsers -> OCR if needed -> Bedrock classification/extraction -> Pydantic -> rules
                                                                        |
Browser -> API -> presigned S3 upload -> EventBridge -> SQS -> Fargate worker
           |                                                 |
           |                      Redis cache -> DynamoDB lease -> Postgres unique record
           |                                                           |
           +-> /review/ -> audited, version-checked human corrections --+
                                                                       |
                                                 reviewed evaluation answer keys

Operator -> validated-field Parquet snapshot -> encrypted S3 -> Glue/Athena
Operator -> Salesforce dry run -> explicit synthetic-data upsert

Safe structured logs + CloudWatch EMF -> dashboard and alarms
CloudTrail -> separate encrypted audit bucket; IAM Access Analyzer -> access findings
```

## Boundaries and entry points

| Boundary | Entry point | Contract |
|---|---|---|
| Untrusted documents | `parsers/router.py`, `pipeline.py` | Content is data, never an instruction; Pydantic and deterministic rules gate acceptance. |
| Processing and deduplication | `service.py` | Same bytes plus the processing recipe identify a result; SQL is the source of truth. |
| API and review | `api/app.py`, `/review/` | Token/OIDC roles, audited reads and changes, optimistic locking, no browser storage. |
| Queue | `worker.py` | Visibility heartbeat, retry backoff, graceful shutdown, poison-message handling. |
| Storage evolution | `storage/alembic/` | Frozen, versioned migrations; legacy adoption is explicit and schema-checked. |
| Telemetry | `logging_safe.py`, `observability.py` | Known metadata only, no free-form messages, filenames, extracted values, or traceback contents. |
| External exports | `analytics.py`, `integrations/salesforce.py` | Operator-triggered only. Never an automatic pipeline side effect. |
| Delivery | `.github/workflows/`, `scripts/finish_deployment.py` | PRs cannot deploy; deployment is manual, main-only, environment-approved, with migration/rollout checks. |

## Phase map

| Phase | Implementation |
|---|---|
| 0–1 | Python package, synthetic generator, parsers, Bedrock tool use, typed schemas, rules, CLI. |
| 2 | Precision/recall/F1, error analysis, comparisons, cost/latency, configurable quality gates. |
| 3 | Tesseract, Textract, cascading OCR and review gating. |
| 4 | SQL records/audits/reviews, Redis deduplication, DynamoDB/SQL leases, API. |
| 5 | Private AWS stack, S3/EventBridge/SQS, Fargate, Docker and release tooling. |
| 6 | Opaque names, fail-closed logs, safe validation errors, OIDC, CloudTrail, Access Analyzer, gateway endpoint policies. |
| 7 | CI, manual OIDC deployments/evaluation, coverage gate, migrations, EMF dashboard/alarms, Jenkins equivalent. |
| 8 | Review UI, Parquet exports, optional Glue/Athena, explicit Salesforce upserts, offline demo and ADRs. |

Step Functions is an optional alternative to the working queue/worker design, not an additional required processing path.
