# Security and real-PHI readiness

**Synthetic data only.** These controls are a reference implementation, not a HIPAA certification.

## Implemented controls

- No public load balancer, internet gateway or NAT. Private service endpoints, scoped task roles, encrypted stores.
- Separate CloudTrail audit bucket, log-file validation, management/S3 data events, VPC flow logs and app audit rows.
- Opaque names for new uploads/records; temporary parser paths never use client filenames.
- Structured logging allow-list. Free-form text, SQL exceptions, tracebacks, URLs and credentials are not rendered.
- OIDC signature/issuer/audience/time verification with explicit roles, or constant-time static-token comparisons.
- Safe request validation errors, no-store headers, UI CSP, no third-party UI assets or browser token persistence.
- Reviewer decisions enforce deterministic rules and optimistic locking.
- External integrations require explicit operator action. Salesforce execution additionally requires synthetic-data acknowledgement.

## Limits that still matter

1. Confirm a BAA and HIPAA eligibility for **every** chosen service/model and region before even considering real PHI.
2. No anonymization guarantee: a prompt forbidding patient identifiers does not prove extracted summaries/names are clean.
   Free-text outputs and review notes need a reviewed retention/access policy and DLP/redaction tests before real use.
3. Existing filename-bearing rows/S3 objects are not scrubbed by schema migration. Plan and approve a separate data migration.
4. RDS app tasks still use the managed master credentials. Provision least-privilege app/review/export DB principals.
5. Static API/Redis secret rotation and automated OIDC token renewal are not implemented.
6. Add deployment-specific rate limiting, WAF/public ingress controls if needed, interface endpoint policies and
   IAM Access Analyzer review. Creating an analyzer is not evidence that its findings are clean.
7. OIDC key refresh requires approved private-network connectivity. API startup configuration is not a networking solution.
8. Application audit rows are not immutable/WORM. CloudTrail S3 versioning/log validation does not equal Object Lock.
   Add retention/legal-hold controls and tamper monitoring if your policy requires them.
9. Pin dependency hashes, action SHAs and base/scanner image digests, review the SBOM, and configure vulnerability triage.
10. Load-test OCR/task resources and service limits; rehearse restore, rollback, DLQ redrive and disaster recovery.

## Secret handling

Use AWS SSO/OIDC and Secrets Manager, never credentials in git/tfvars. Keep Terraform state encrypted and tightly scoped,
because it contains the generated Redis token. Do not archive plans/state, environment files, raw documents or real
evaluation datasets in CI. Synthetic reports are the only exported CI artifacts.

Application logs intentionally lose unstructured diagnostics. Investigate via bounded event/error-class metadata,
audited record IDs and secured database access rather than enabling document dumps or third-party debug logging.
