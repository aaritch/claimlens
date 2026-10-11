# Terraform state bootstrap

This root creates a private, versioned S3 state bucket and a dedicated rotating KMS key.
It is separate from the application stack and protects the bucket/key from Terraform destruction.
Current state is retained indefinitely; noncurrent versions expire after 90 days.

Use Terraform 1.10 or newer (CI uses 1.16.5), an explicitly supplied `account_id`, and a secured local directory for this
bootstrap root's local state and saved plan. Initialize with a local backend path outside the repository:

```text
terraform -chdir=infra/state-bootstrap init -backend-config=path=<private-local-path>/state-bootstrap.tfstate
terraform -chdir=infra/state-bootstrap plan -var=account_id=<expected-account> -out=<private-local-path>/state-bootstrap.tfplan
```

Do not apply until the account, new resource names and costs have been approved.
After applying, configure the application S3 backend with `state_bucket`, `state_kms_key_arn`,
`encrypt=true`, `use_lockfile=true`, and `allowed_account_ids=[<expected-account>]`.
Copy `infra/terraform/backend.tf.example` to `backend.tf`, keep environment-specific settings in a protected copy
of `infra/terraform/backend.hcl.example`, and initialize the application with `-backend-config=<absolute-private-path>`.
Use the full KMS key ARN from the output, not the alias: the write policy compares the explicit encryption key header.
The backend sends explicit SSE-KMS headers; the bucket refuses unencrypted/wrong-key writes.
Restrict S3/KMS access to approved deployment principals through IAM. This root does not create deployment roles.
Keep the bootstrap's local state protected and backed up. Never publish state/plan files to GitHub,
CI artifacts, or other destinations outside the approved state-storage workflow.
