"""Offline security invariants for the state root; live service checks remain operator responsibilities."""
from pathlib import Path

import pytest

hcl2 = pytest.importorskip("hcl2")
ROOT = Path(__file__).resolve().parents[2] / "infra" / "state-bootstrap"


@pytest.fixture(scope="module")
def bootstrap():
    with (ROOT / "main.tf").open(encoding="utf-8") as source:
        return hcl2.load(source)


def block(config, category, kind, name):
    return next(entry[kind][name] for entry in config[category] if kind in entry and name in entry[kind])


def test_bootstrap_authentication_is_bound_to_expected_account(bootstrap):
    provider = next(entry["aws"] for entry in bootstrap["provider"] if "aws" in entry)
    assert provider["allowed_account_ids"] == ["${var.account_id}"]
    account = next(entry["account_id"] for entry in bootstrap["variable"] if "account_id" in entry)
    assert "default" not in account
    assert "^[0-9]{12}$" in account["validation"][0]["condition"]


@pytest.mark.parametrize("kind", ["aws_s3_bucket", "aws_kms_key"])
def test_state_bucket_and_key_cannot_be_accidentally_destroyed(bootstrap, kind):
    resource = block(bootstrap, "resource", kind, "state")
    assert resource["lifecycle"][0]["prevent_destroy"] is True
    if kind == "aws_s3_bucket":
        assert resource["force_destroy"] is False
    else:
        assert resource["enable_key_rotation"] is True
        assert resource["deletion_window_in_days"] == 30


def test_state_bucket_is_private_owned_and_versioned(bootstrap):
    public = block(bootstrap, "resource", "aws_s3_bucket_public_access_block", "state")
    for setting in ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"):
        assert public[setting] is True
    ownership = block(bootstrap, "resource", "aws_s3_bucket_ownership_controls", "state")
    assert ownership["rule"][0]["object_ownership"] == "BucketOwnerEnforced"
    versions = block(bootstrap, "resource", "aws_s3_bucket_versioning", "state")
    assert versions["versioning_configuration"][0]["status"] == "Enabled"


def test_state_encryption_uses_the_dedicated_key(bootstrap):
    encryption = block(bootstrap, "resource", "aws_s3_bucket_server_side_encryption_configuration", "state")
    default = encryption["rule"][0]["apply_server_side_encryption_by_default"][0]
    assert default["sse_algorithm"] == "aws:kms"
    assert default["kms_master_key_id"] == "${aws_kms_key.state.arn}"


def test_state_bucket_denies_insecure_unencrypted_and_wrong_key_writes(bootstrap):
    policy = block(bootstrap, "data", "aws_iam_policy_document", "state_bucket")
    statements = {statement["sid"]: statement for statement in policy["statement"]}
    assert set(statements) == {"DenyInsecureTransport", "RequireKmsOnWrites", "RequireTheStateKey"}
    for statement in statements.values():
        assert statement["effect"] == "Deny"
        assert statement["principals"][0] == {"type": "*", "identifiers": ["*"]}
    assert statements["DenyInsecureTransport"]["condition"][0] == {
        "test": "Bool", "variable": "aws:SecureTransport", "values": ["false"],
    }
    assert statements["RequireKmsOnWrites"]["condition"][0]["values"] == ["aws:kms"]
    assert statements["RequireTheStateKey"]["condition"][0]["values"] == ["${aws_kms_key.state.arn}"]


def test_state_retention_never_expires_the_current_snapshot(bootstrap):
    lifecycle = block(bootstrap, "resource", "aws_s3_bucket_lifecycle_configuration", "state")
    assert lifecycle["depends_on"] == ["${aws_s3_bucket_versioning.state}"]
    rule = lifecycle["rule"][0]
    assert "expiration" not in rule
    assert rule["noncurrent_version_expiration"][0]["noncurrent_days"] == 90


def test_deployment_configuration_and_state_are_git_ignored():
    application_ignore = (ROOT.parent / "terraform" / ".gitignore").read_text().splitlines()
    bootstrap_ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert {"backend.tf", "backend*.hcl", "*.tfvars", "*.tfvars.json", "*.tfstate", "*.tfplan"} <= set(application_ignore)
    assert {".terraform/", "*.tfstate", "*.tfstate.*", "*.tfplan", "*.tfvars", "*.tfvars.json"} <= set(bootstrap_ignore)
