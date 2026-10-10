# Operations runbook

## Local checks

Run `ruff check claimlens scripts tests`, `mypy`, `pytest --cov=claimlens`,
`terraform -chdir=infra/terraform fmt -check -recursive`, then `init -backend=false` and `validate`.
Schema validation needs no AWS account but does download providers. It does not replace a live `plan`.

The image job also runs `scripts/container_smoke.py` as UID 10001, with a read-only filesystem,
temporary `/tmp`, no published ports and `--network none`. It verifies runtime-only imports, migrations,
API/UI assets, duplicate protection, review, Salesforce dry run and real local Tesseract OCR.

## First deployment

1. Set a Budget and alert recipient. Approve costs, retention, region, models and allowed network ranges.
2. Provision secured remote state and account-specific OIDC roles separately.
3. Bootstrap services disabled. Configure auth; put static-token secrets out-of-band or confirm OIDC/JWKS reachability.
4. Request deployment on main. Offline CI and the separately approved/billed real-Bedrock quality gate must pass
   before the read-only plan. Review provider/network/IAM/KMS changes.
5. Approve deployment. The release builds/scans before publishing a SHA tag, applies reviewed IaC,
   runs `db-migrate`, checks the migration container's exit code, and waits for ECS stability.
   A circuit-breaker rollback is reported as failure, not success.
6. Validate CloudTrail delivery and log validation; confirm S3 data events and review Access Analyzer findings.
7. Test one synthetic upload end-to-end, one multi-page OCR document, a duplicate, a transient retry,
   reviewer role enforcement and a stale-version conflict.
8. Verify the dashboard receives EMF metrics and confirm the SNS subscription.

`scripts/deploy.sh` remains a Bash operator tool. Its release/migrate commands now wait for actual migration
completion and service stability, and no longer execute a Terraform output through shell `eval`.
Run release only from a committed tree. Keep migration changes backward compatible with active old workers.

## Alarm response

| Alarm | Check | Action |
|---|---|---|
| Dead-letter queue nonempty | Worker events, receive count, model access/throttling, object lifecycle | Fix the cause, then explicitly redrive a small batch. Never blindly purge. |
| Oldest queued message | Worker task count/health, heartbeat, model/Textract limits | Restore workers or cautiously scale within measured limits. |
| EventBridge failure | Source prefix, target queue policy, cross-service KMS permission | Repair delivery; confirm a fresh synthetic upload arrives. |
| Processing errors | Safe error-class events, corrupt/type-invalid inputs, OCR/model availability | Separate permanent bad files from transient service failures. |
| Cost per document | Pricing config, token/OCR volume, retry count, model selection | Verify prices and reduce workload/model cost; use Budget alerts as an independent guardrail. |

Duplicate uploads do not emit another processing metric. Retries do, so attempted-processing cost can rise even
when the number of stored records stays flat. Missing metrics are not treated as alarm breaches; ECS/queue health
must be monitored independently.

## Migration and rollback

Back up and test restores before adopting a legacy schema or modifying production tables. Legacy adoption
must pass schema comparison; do not force-stamp a mismatch. Do not use `db-init` as a production migration tool.

The initial revision includes a destructive downgrade for isolated test databases only. No downgrade CLI is
exposed. Production rollback should normally redeploy a previously verified image and preserve the additive schema.
Do not destroy a database or remove columns to undo a release without a separate reviewed recovery plan.

## Export recovery

Parquet exports refuse overwrite. If a run fails midway, choose a new path and investigate the incomplete file;
do not assume it is a complete snapshot. Publish only a completed audited export.
Salesforce retries use the same UUID external ID. A crash after a successful remote PATCH but before the local
audit commit may leave a remote result without an audit row; retry explicitly to converge and audit it.
