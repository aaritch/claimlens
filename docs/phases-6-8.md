# Phases 6–8

## Install and verify

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\ruff.exe check claimlens scripts tests
.\.venv\Scripts\mypy.exe
.\.venv\Scripts\python.exe -m pytest --cov=claimlens
```

The base image installs `requirements.txt` only. Parquet export additionally needs `requirements-analytics.txt`.
Development requirements include it for offline tests. Type checking currently covers the new maintained modules
listed in `pyproject.toml`, not the entire pre-existing package. Lint checks target correctness, not formatting.

Optional real-browser checks use `requirements-browser.txt` and a fresh, isolated browser profile:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-browser.txt
.\.venv\Scripts\python.exe -m playwright install chromium
$env:CLAIMLENS_BROWSER_TESTS = "1"
# To use an already installed browser with a NEW profile instead of downloading Chromium:
# $env:CLAIMLENS_BROWSER_CHANNEL = "msedge"
.\.venv\Scripts\python.exe -m pytest tests/test_review_browser.py
```

CI installs Chromium and enables these tests. They bind only to localhost, use temporary synthetic SQLite data,
and exercise approvals, corrections, validation, rejections, stale versions, disconnect and mobile layout.

## Phase 6: security

- New source names and S3 upload keys are opaque by default. Original user filenames are not persisted in new rows.
- Logging renders only allow-listed events and metadata. Unknown/free-form logs and exception tracebacks are suppressed.
  Worker events still distinguish processing, retries, queue errors and bookkeeping errors.
- Request validation responses omit rejected input values.
- Optional OIDC accepts RS256 ID tokens only, verifies signature, issuer, audience, expiry, issued-at and subject,
  and maps known roles from `cognito:groups`. Unknown/missing roles are denied, not downgraded to reader.
- The Terraform stack adds CloudTrail management and documents-bucket object events, log validation,
  an encrypted audit bucket, IAM Access Analyzer, and S3/DynamoDB gateway endpoint policies.

OIDC configuration:

```text
CLAIMLENS_OIDC_ISSUER=https://your-approved-issuer
CLAIMLENS_OIDC_AUDIENCE=your-client-id
CLAIMLENS_OIDC_JWKS_URL=https://your-approved-issuer/your-jwks-path
CLAIMLENS_OIDC_ROLES_CLAIM=cognito:groups
```

Use the provider's **ID token**, not an opaque OAuth access token. Public Cognito/JWKS URLs are not assumed reachable
from the no-NAT VPC. Set up an approved private identity-provider path before enabling OIDC in the deployed API.
Static-token auth remains the default; disabling auth is for isolated local demos only.

## Database migrations

```powershell
$env:CLAIMLENS_DATABASE_URL = "sqlite:///claimlens.db"
.\.venv\Scripts\python.exe -m claimlens db-migrate
```

For an existing `db-init` database, first take and test a backup, then run `db-migrate --adopt-legacy`.
Adoption refuses a schema that differs from the baseline. It preserves existing records, including their old filenames.
The runtime explicitly fixes classification confidence to `Float` so SQLAlchemy's evolving type inference cannot
change the schema silently. Future revisions must use frozen definitions, not current application model imports.
Use expand/contract, backward-compatible migrations while old workers are still running.

## Phase 7: delivery and monitoring

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | PR, main push, manual, reusable call | Lint, selected-module type checks, tests/80% coverage, offline evaluation gate, application/state-bootstrap Terraform validation, Docker/Trivy scan. |
| `deploy.yml` | Manual, main only | Re-run CI and require protected real-model evaluation, then read-only plan, approved production deployment via OIDC, git-SHA image, schema migration, ECS stability and rollback detection. |
| `evaluate.yml` | Manual or reusable deployment call, main only | Separate environment-approved, billed Bedrock evaluation on synthetic held-out data. |
| `Jenkinsfile` | Operator-configured | Equivalent local checks, evaluation, infrastructure validation, build and scan. It does not provision Jenkins or embed AWS credentials. |

Before enabling deployment:

1. Use Terraform 1.10+ (CI pins 1.16.5), provision state storage with `infra/state-bootstrap`,
   then bootstrap the application with services disabled so the ECR repository exists.
2. Create GitHub environments `aws-plan`, `production`, and `model-evaluation`.
3. Require reviewers and main-only branch restrictions for production and model evaluation.
4. Configure short-lived AWS OIDC roles. Pin trust to your repository and each exact environment subject.
   Do not grant deployment privileges to pull-request subjects.
5. Set environment variables `AWS_REGION`, `AWS_PLAN_ROLE_ARN` / `AWS_DEPLOY_ROLE_ARN` / `AWS_EVAL_ROLE_ARN`,
   `TF_BACKEND_HCL` (a complete non-secret S3 backend block, not the partial local declaration),
   `TF_VARS_JSON` (non-secret Terraform inputs, including the expected `allowed_account_ids`),
   and `CLAIMLENS_MODEL_ID` in model evaluation.
6. Protect Terraform state with encryption, access controls and locking. Never upload binary plans/state as artifacts.
7. Configure alert subscriptions, replace placeholder prices, and set an AWS Budget.
8. Review and pin action/image/dependency digests for production. Release tags are used for the initial workflow scaffold.

The plan role needs state reads/locking and AWS describe/read permissions, but no infrastructure writes.
The deployment role needs reviewed IaC permissions, ECR publishing, ECS task operations and narrowly scoped `iam:PassRole`.
These account-specific roles are not automatically granted by this repository.

The offline mock gate validates deterministic plumbing, **not prompt/model quality**. A manually requested
deployment must also pass the protected, billed real-Bedrock workflow before planning/applying.
It can also be run separately before promoting model/prompt changes; hold the dataset fixed and
keep a separately seeded final holdout. Generated datasets are reproducible, but not immutable fixtures.

EMF is enabled in the Fargate worker with `CLAIMLENS_METRICS=1`; local default is off. Metrics include documents,
tokens, OCR pages/routing, latency, validation routing, processing errors and estimated cost.
Dimensions use environment only, never document IDs or field values. Cost alarms count attempted processing,
including transient retries, not just successfully stored documents. Duplicates emit no processing metrics.
An output failure cannot fail document processing.

## Phase 8: review UI

```powershell
$env:CLAIMLENS_LLM = "mock:careful"
$env:CLAIMLENS_API_AUTH = "disabled" # localhost synthetic demo only
.\.venv\Scripts\python.exe -m claimlens db-migrate
.\.venv\Scripts\python.exe -m claimlens serve
```

Open `http://127.0.0.1:8080/review/`. The static shell contains no claims. Every data request uses the API's existing
auth and audit trail. Tokens stay in memory, never local/session storage; disconnect clears the displayed data.
Use a reviewer token for corrections. Approval cannot bypass required fields or hard validation rules.
On a 409 conflict, reload the document rather than blindly resubmitting a newer version.

## Parquet and Athena

```powershell
.\.venv\Scripts\python.exe -m claimlens export-analytics --out exports/snapshot-001.parquet
```

Only validated business fields and numeric metrics are exported, in batches of 500. Provider names, filenames,
summaries, raw model output, notes and reviewer identity are excluded. Currency is written as fixed-precision decimal.
Existing exports are never overwritten. Restrict access to the export directory.

Set Terraform `enable_analytics=true` to add the private KMS-encrypted analytics bucket, Glue database/table, and Athena
workgroup (enforced encrypted results, 1 GiB query scan cap). An approved operator can explicitly upload:

```text
python -m claimlens export-analytics --out exports/snapshot-002.parquet --upload-bucket <analytics-bucket> --key exports/run-002/claims.parquet --kms-key <approved-key-arn>
```

This is a real external write; do it only with approval and synthetic data. The exporter needs only `s3:PutObject`
on `exports/*` and the needed key permissions. An analyst additionally needs Athena execution permissions,
Glue reads and access to exports/query results. Neither role is added to the worker or API.
Query from an approved analyst environment; the worker VPC does not provision Athena/Glue interface endpoints.

`docs/sql/analytics.sql` contains a latest-version view and example payer/cost/latency queries.
Repeated full snapshots contain repeated IDs; use the view, not raw `COUNT(*)`.
This validated-only dataset cannot measure rejection rates; use CloudWatch routing metrics for that.

## Salesforce

Create a synthetic-only Developer Edition custom object `Claim__c`. Configure:

- `Document_ID__c`: unique external-ID text field, at least 36 characters.
- `Document_Type__c`, `Provider_NPI__c`, `Payer_Name__c`: text.
- `Service_Date__c`: date.
- `Disputed_Amount__c`: currency with two decimals.

Preview is offline, prints only field names and document ID, and needs no Salesforce credentials:

```text
python -m claimlens sync-salesforce --document-id <validated-document-id>
```

An explicitly approved operator can set `CLAIMLENS_SALESFORCE_URL` (HTTPS My Domain),
`CLAIMLENS_SALESFORCE_TOKEN` through their secret store, and execute with both
`--execute --synthetic-data-only`. The token must never be placed in command-line arguments or committed files.
`CLAIMLENS_SALESFORCE_OBJECT`, `CLAIMLENS_SALESFORCE_EXTERNAL_ID` and
`CLAIMLENS_SALESFORCE_API_VERSION` override API names/version (default `v61.0`).

Only validated claims are sent. PATCH upsert uses the document UUID as external ID, making explicit retries
idempotent. Redirects are refused; errors do not echo remote response bodies or tokens. Re-run transient
failures explicitly. There is no automatic export, token-refresh service, or Salesforce egress from the private worker.
Live credentials, org setup and remote smoke tests are operator responsibilities.

## Local validation, October 10, 2026

- Windows/Python 3.14 suite: **360 passed, 8 skipped**, **93.34%** statement coverage.
- Real-browser tests ran in fresh Edge profiles against isolated localhost SQLite instances.
- The skips cover absent local Redis/DynamoDB race services and the Windows Tesseract executable.
  Real Tesseract was separately verified inside the Linux production image.
- Ruff, selected-module mypy, Terraform formatting/provider initialization/schema validation, and offline quality gates passed.
- Linux/amd64 production image built; runtime-only, non-root/read-only/no-network smoke tests passed on Python 3.12.
- Trivy 0.65.0 found **zero fixable HIGH/CRITICAL findings** using `--severity HIGH,CRITICAL --ignore-unfixed`.
  This is not a claim that lower-severity/unfixed vulnerabilities are absent.
- These application/image checks were offline; live model/OCR, hosted deployments, OIDC and Salesforce writes were not run.

Dependency deprecation and older test-fixture SQLite cleanup warnings remain non-failing. The image and local
validation artifacts remain local and are not committed.

## Infrastructure-only AWS validation, October 10, 2026

The approved development deployment created **9 state-bootstrap and 102 application Terraform entries**.
It verified private/encrypted storage and queues, an active encrypted DynamoDB table, available PostgreSQL
and Redis with TLS/auth, nine private interface endpoints across two subnets plus two gateway endpoints,
CloudTrail logging/delivery, and versioned KMS-encrypted remote state with S3-native locking.

AWS reports PostgreSQL 16's already-enabled `rds.force_ssl` system default with a `pending-reboot` apply method.
Terraform now matches that metadata while keeping the value at `1`; the database was in sync and was not rebooted.
The final application plan reported **no changes**.

Worker/API/analytics remain disabled. ECR has no published application image; migrations and end-to-end
application tests have not run. API certificate/network/auth choices and the approved real-model quality gate
must be resolved before a later activation. Account-specific inputs, backend settings, state and saved plans
are excluded from Git; no deployment credentials belong in the repository.
