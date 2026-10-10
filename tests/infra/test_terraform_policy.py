"""Policy-as-code for infra/terraform.

IMPORTANT: this is STATIC analysis of the HCL. Nothing here talks to AWS or the Terraform provider, so it cannot
catch an invalid attribute name, a quota, or a cross-service permission mistake. That needs a real
`terraform validate` / `plan` / `apply` (see the first-deploy checklist in the README). What it DOES guarantee is that
the security invariants we care about can't be silently weakened, and that every reference resolves.
"""
import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

hcl2 = pytest.importorskip("hcl2")
TF_DIR = Path(__file__).resolve().parents[2] / "infra" / "terraform"
pytestmark = pytest.mark.skipif(not TF_DIR.exists(), reason="infra/terraform not present")

FILES = sorted(TF_DIR.glob("*.tf"))


# ---- helpers ---------------------------------------------------------------------------------------------
def attr(block, key, default=None):
    v = block.get(key)
    return default if v is None else v


def parse(path):
    with open(path) as fh:
        return hcl2.load(fh)


@pytest.fixture(scope="module")
def tf():
    merged = {"resource": [], "data": [], "variable": [], "locals": [], "output": [], "terraform": [], "provider": []}
    for f in FILES:
        for k, v in parse(f).items():
            merged.setdefault(k, []).extend(v)
    resources, data, variables, locs = {}, {}, {}, {}
    for entry in merged["resource"]:
        for rtype, named in entry.items():
            for name, body in named.items():
                resources.setdefault(rtype, {})[name] = body
    for entry in merged["data"]:
        for dtype, named in entry.items():
            for name, body in named.items():
                data.setdefault(dtype, {})[name] = body
    for entry in merged["variable"]:
        variables.update(entry)
    for entry in merged["locals"]:
        locs.update({k: v for k, v in entry.items() if not k.startswith("__")})
    return {"resources": resources, "data": data, "variables": variables, "locals": locs, "raw": merged}


def code_lines():
    out = []
    for f in FILES:
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if not line.strip().startswith("#"):
                out.append((f.name, i, line))
    return out


def statements(doc):
    return doc.get("statement", [])


def stmt_effect(st):
    return attr(st, "effect", "Allow")


# ---- 1. syntax and references -------------------------------------------------------------------------------
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_file_parses(path):
    assert parse(path) is not None


def test_every_reference_resolves_to_something_declared(tf):
    """A poor man's `terraform validate`: catches typos in resource names, variables and locals."""
    declared_res = {(t, n) for t, named in tf["resources"].items() for n in named}
    declared_data = {(t, n) for t, named in tf["data"].items() for n in named}
    pattern = re.compile(r'(?<![\w."])(data\.)?((?:aws|random)_[a-z0-9_]+)\.([A-Za-z_][A-Za-z0-9_-]*)')
    unresolved = []
    for fname, lineno, line in code_lines():
        for m in pattern.finditer(line):
            is_data, rtype, name = m.group(1), m.group(2), m.group(3)
            known = declared_data if is_data else declared_res
            if (rtype, name) not in known:
                unresolved.append(f"{fname}:{lineno} {'data.' if is_data else ''}{rtype}.{name}")
        for m in re.finditer(r'\bvar\.([A-Za-z_][A-Za-z0-9_]*)', line):
            if m.group(1) not in tf["variables"]:
                unresolved.append(f"{fname}:{lineno} var.{m.group(1)}")
        for m in re.finditer(r'\blocal\.([A-Za-z_][A-Za-z0-9_]*)', line):
            if m.group(1) not in tf["locals"]:
                unresolved.append(f"{fname}:{lineno} local.{m.group(1)}")
    assert not unresolved, "\n".join(unresolved)


def test_every_variable_is_documented_and_safe_defaults_hold(tf):
    undocumented = [n for n, b in tf["variables"].items() if "description" not in b]
    assert not undocumented, undocumented
    default = lambda n: attr(tf["variables"][n], "default")
    assert default("deletion_protection") is True        # destructive things are opt-in
    assert default("force_destroy") is False
    assert default("enable_services") is False           # two-phase rollout: image must exist first
    assert default("enable_api") is False
    assert default("api_allowed_cidrs") == []            # no implicit network exposure


def test_formatting_is_canonical():
    binary = os.getenv("TOFU_BIN") or shutil.which("tofu") or shutil.which("terraform") or ("/tmp/tofu" if Path("/tmp/tofu").exists() else None)
    if not binary:
        pytest.skip("no tofu/terraform binary available")
    result = subprocess.run([binary, "fmt", "-check", "-recursive", str(TF_DIR)], capture_output=True, text=True)
    assert result.returncode == 0, f"unformatted files:\n{result.stdout}"


# ---- 2. nothing is exposed to the internet ----------------------------------------------------------------------
def test_no_open_cidrs_and_no_internet_path(tf):
    offenders = [f"{f}:{n}" for f, n, line in code_lines() if "0.0.0.0/0" in line or "::/0" in line]
    assert not offenders, offenders
    for forbidden in ("aws_internet_gateway", "aws_nat_gateway", "aws_eip", "aws_route"):
        assert forbidden not in tf["resources"], f"{forbidden} would create a path to the internet"


def test_ingress_cidrs_come_only_from_the_vpc_or_the_explicit_allow_list(tf):
    for name, body in tf["resources"]["aws_vpc_security_group_ingress_rule"].items():
        cidr = attr(body, "cidr_ipv4")
        if cidr:
            assert cidr in ("${var.vpc_cidr}", "${var.api_allowed_cidrs[count.index]}"), (name, cidr)


def test_required_vpc_endpoints_exist(tf):
    r = tf["resources"]["aws_vpc_endpoint"]
    assert {"s3", "dynamodb", "interface"} <= set(r)
    assert attr(r["s3"], "vpc_endpoint_type") == "Gateway" and attr(r["dynamodb"], "vpc_endpoint_type") == "Gateway"
    services = set(re.findall(r"[a-z][a-z0-9.\-]*", str(tf["locals"]["interface_endpoints"])))
    for needed in ("ecr.api", "ecr.dkr", "logs", "sqs", "kms", "sts", "secretsmanager", "bedrock-runtime", "textract"):
        assert needed in services, f"missing VPC endpoint for {needed}: tasks would hang trying to reach it"


def test_vpc_has_flow_logs_and_a_locked_default_security_group(tf):
    assert "aws_flow_log" in tf["resources"] and "aws_default_security_group" in tf["resources"]
    assert not [k for k in tf["resources"]["aws_default_security_group"]["default"] if k in ("ingress", "egress")]


# ---- 3. encryption everywhere ---------------------------------------------------------------------------------------
def test_kms_key_rotates_and_the_policy_names_only_expected_principals(tf):
    key = tf["resources"]["aws_kms_key"]["main"]
    assert attr(key, "enable_key_rotation") is True
    principals = set()
    for st in statements(tf["data"]["aws_iam_policy_document"]["kms"]):
        for p in st.get("principals", []):
            principals.update(attr(p, "identifiers"))
    assert principals == {"arn:aws:iam::${local.account_id}:root", "logs.${var.aws_region}.amazonaws.com",
                          "events.amazonaws.com", "cloudwatch.amazonaws.com", "cloudtrail.amazonaws.com"}


def bucket_children(tf, bucket):
    found = {}
    for rtype, named in tf["resources"].items():
        if rtype.startswith("aws_s3_bucket_") and rtype != "aws_s3_bucket":
            for n, body in named.items():
                if f"aws_s3_bucket.{bucket}." in str(attr(body, "bucket", "")):
                    found[rtype] = body
    return found


@pytest.mark.parametrize("bucket", ["documents", "access_logs", "audit"])
def test_every_bucket_is_private_versioned_encrypted_tls_only_and_lifecycle_managed(tf, bucket):
    kids = bucket_children(tf, bucket)
    for needed in ("aws_s3_bucket_public_access_block", "aws_s3_bucket_versioning",
                   "aws_s3_bucket_server_side_encryption_configuration", "aws_s3_bucket_policy",
                   "aws_s3_bucket_ownership_controls", "aws_s3_bucket_lifecycle_configuration"):
        assert needed in kids, f"{bucket} lacks {needed}"
    pab = kids["aws_s3_bucket_public_access_block"]
    assert all(attr(pab, k) is True for k in ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"))
    policy_doc = tf["data"]["aws_iam_policy_document"][f"{bucket}_bucket"]
    assert any(stmt_effect(s) == "Deny" and "aws:SecureTransport" in json.dumps(s) for s in statements(policy_doc))


def test_documents_bucket_uses_the_customer_key_and_triggers_eventbridge(tf):
    kids = bucket_children(tf, "documents")
    sse = kids["aws_s3_bucket_server_side_encryption_configuration"]
    rule = sse["rule"][0]
    default = rule["apply_server_side_encryption_by_default"][0]
    assert attr(default, "sse_algorithm") == "aws:kms" and "aws_kms_key.main" in attr(default, "kms_master_key_id")
    assert attr(tf["resources"]["aws_s3_bucket_notification"]["documents"], "eventbridge") is True
    assert "aws_s3_bucket_logging" in kids


def test_queues_are_encrypted_and_failures_are_captured(tf):
    q = tf["resources"]["aws_sqs_queue"]
    for name in ("main", "dlq"):
        assert "aws_kms_key.main" in attr(q[name], "kms_master_key_id"), name
    redrive = attr(q["main"], "redrive_policy")
    assert "aws_sqs_queue.dlq.arn" in redrive and "var.max_receive_count" in redrive
    assert attr(q["main"], "receive_wait_time_seconds") == 20


def test_upload_events_are_filtered_by_prefix_and_only_our_rule_may_publish(tf):
    pattern = attr(tf["resources"]["aws_cloudwatch_event_rule"]["uploads"], "event_pattern")
    assert "prefix" in pattern and "var.upload_prefix" in pattern and "Object Created" in pattern
    pub = [s for s in statements(tf["data"]["aws_iam_policy_document"]["queue"]) if "sqs:SendMessage" in attr(s, "actions")][0]
    assert "aws:SourceArn" in json.dumps(pub) and "aws_cloudwatch_event_rule.uploads.arn" in json.dumps(pub)


def test_alarms_exist_for_every_silent_failure_mode(tf):
    alarms = tf["resources"]["aws_cloudwatch_metric_alarm"]
    for needed in ("dlq_not_empty", "queue_stalled", "events_failing"):
        assert needed in alarms and attr(alarms[needed], "alarm_actions") == ["${aws_sns_topic.alerts.arn}"]
    assert "aws_kms_key.main" in attr(tf["resources"]["aws_sns_topic"]["alerts"], "kms_master_key_id")


def test_idempotency_table_has_ttl_pitr_and_cmk(tf):
    t = tf["resources"]["aws_dynamodb_table"]["idempotency"]
    assert attr(t["ttl"][0], "enabled") is True and attr(t["ttl"][0], "attribute_name") == "expires_at"
    assert attr(t["point_in_time_recovery"][0], "enabled") is True
    assert attr(t["server_side_encryption"][0], "enabled") is True and "aws_kms_key.main" in attr(t["server_side_encryption"][0], "kms_key_arn")
    assert attr(t, "hash_key") == "pk" and attr(t, "deletion_protection_enabled") == "${var.deletion_protection}"


def test_database_is_private_encrypted_tls_only_and_has_no_password_in_code(tf):
    db = tf["resources"]["aws_db_instance"]["main"]
    assert attr(db, "storage_encrypted") is True and attr(db, "publicly_accessible") is False
    assert attr(db, "manage_master_user_password") is True
    assert "password" not in db, "the master password must be RDS-managed, never in code or state"
    assert attr(db, "iam_database_authentication_enabled") is True and attr(db, "deletion_protection") == "${var.deletion_protection}"
    params = tf["resources"]["aws_db_parameter_group"]["postgres"]["parameter"]
    assert any(attr(p, "name") == "rds.force_ssl" and attr(p, "value") == "1" for p in params)


def test_redis_encrypts_at_rest_and_in_transit_and_requires_auth(tf):
    r = tf["resources"]["aws_elasticache_replication_group"]["main"]
    assert attr(r, "at_rest_encryption_enabled") is True and attr(r, "transit_encryption_enabled") is True
    assert attr(r, "auth_token") and "random_password" in attr(r, "auth_token")


def test_logs_ecr_and_secrets_use_the_customer_key(tf):
    for name, body in tf["resources"]["aws_cloudwatch_log_group"].items():
        assert "aws_kms_key.main" in attr(body, "kms_key_id") and "var.log_retention_days" in str(attr(body, "retention_in_days")), name
    repo = tf["resources"]["aws_ecr_repository"]["app"]
    assert attr(repo, "image_tag_mutability") == "IMMUTABLE"
    assert attr(repo["image_scanning_configuration"][0], "scan_on_push") is True
    assert attr(repo["encryption_configuration"][0], "encryption_type") == "KMS"
    for name, body in tf["resources"]["aws_secretsmanager_secret"].items():
        assert "aws_kms_key.main" in attr(body, "kms_key_id"), name


def test_api_tokens_secret_has_no_value_in_terraform(tf):
    versions = tf["resources"].get("aws_secretsmanager_secret_version", {})
    assert set(versions) == {"redis_auth"}, "api tokens must be set out-of-band, never through Terraform state"


# ---- 4. least privilege ---------------------------------------------------------------------------------------------------
ALLOWED_STAR_RESOURCE_ACTIONS = {"ecr:GetAuthorizationToken", "textract:DetectDocumentText",
                                 "textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection"}


def role_policies(tf):
    return {n: d for n, d in tf["data"]["aws_iam_policy_document"].items() if n in ("execution", "worker", "api", "flow_write")}


def test_no_allow_statement_uses_a_wildcard_action(tf):
    for name, doc in role_policies(tf).items():
        for st in statements(doc):
            if stmt_effect(st) == "Allow":
                for a in attr(st, "actions"):
                    assert "*" not in a, f"{name}: {a}"


def test_wildcard_resources_are_limited_to_actions_aws_cannot_scope(tf):
    for name, doc in role_policies(tf).items():
        for st in statements(doc):
            if "*" in attr(st, "resources"):
                assert set(attr(st, "actions")) <= ALLOWED_STAR_RESOURCE_ACTIONS, (name, attr(st, "sid"))


def actions_of(tf, doc_name):
    return {a for st in statements(tf["data"]["aws_iam_policy_document"][doc_name]) if stmt_effect(st) == "Allow" for a in attr(st, "actions")}


def test_bedrock_access_is_limited_to_the_listed_models(tf):
    st = [s for s in statements(tf["data"]["aws_iam_policy_document"]["worker"]) if "bedrock:InvokeModel" in attr(s, "actions")]
    assert len(st) == 1 and attr(st[0], "resources") == "${var.bedrock_model_arns}"


def test_each_component_has_only_the_powers_it_needs(tf):
    worker, api = actions_of(tf, "worker"), actions_of(tf, "api")
    assert "s3:DeleteObject" in worker and "bedrock:InvokeModel" in worker
    assert not {"s3:GetObject", "bedrock:InvokeModel", "sqs:ReceiveMessage", "dynamodb:PutItem"} & api, "API role must not read documents or call models"
    assert not {"secretsmanager:GetSecretValue", "ecr:GetAuthorizationToken"} & worker, "the app never fetches secrets or images itself; ECS does"
    # the worker may only delete its temporary OCR copies, never the uploaded originals
    for st in statements(tf["data"]["aws_iam_policy_document"]["worker"]):
        if "s3:DeleteObject" in attr(st, "actions"):
            assert all("${local.ocr_temp_prefix}" in r for r in attr(st, "resources"))
    assert "migrate_task" in tf["resources"]["aws_iam_role"] and "migrate_task" not in tf["resources"].get("aws_iam_role_policy", {})


def test_task_roles_can_only_be_assumed_by_ecs_in_this_account(tf):
    doc = tf["data"]["aws_iam_policy_document"]["ecs_tasks_assume"]
    st = statements(doc)[0]
    assert attr(st["principals"][0], "identifiers") == ["ecs-tasks.amazonaws.com"]
    assert "aws:SourceAccount" in json.dumps(st["condition"])


# ---- 5. containers ---------------------------------------------------------------------------------------------------------
def test_containers_are_hardened(tf):
    hardening = json.dumps(tf["locals"]["hardening"])
    assert '"user = ' not in hardening  # (parser shape varies) assert via raw text instead
    text = (TF_DIR / "ecs.tf").read_text()
    assert 'user                   = "10001"' in text and "readonlyRootFilesystem = true" in text
    for task in ("worker", "migrate"):
        assert re.search(rf'resource "aws_ecs_task_definition" "{task}"', text)
    api = (TF_DIR / "api.tf").read_text()
    for body in (text, api):
        assert "merge(local.hardening" in body or "merge(local.hardening_without_healthcheck" in body
    assert "stopTimeout = 120" in text


def test_secrets_are_injected_by_reference_never_as_plain_environment_values():
    text = "".join(f.read_text() for f in FILES)
    for line in text.splitlines():
        if re.search(r'\{\s*name\s*=\s*"[A-Z_]*(PASSWORD|TOKEN|SECRET)[A-Z_]*"\s*,\s*value\s*=', line):
            pytest.fail(f"secret passed as a plain environment value: {line.strip()}")
    assert "valueFrom" in text and ':password::' in text


def test_service_rollout_is_gated_and_rolls_back_bad_images(tf):
    for name in ("worker", "api"):
        svc = tf["resources"]["aws_ecs_service"][name]
        assert "var.enable_services" in attr(svc, "count")
        assert attr(svc["deployment_circuit_breaker"][0], "rollback") is True
        assert attr(svc["network_configuration"][0], "assign_public_ip") is False


def test_load_balancer_is_internal_https_only(tf):
    lb = tf["resources"]["aws_lb"]["api"]
    assert attr(lb, "internal") is True and attr(lb, "drop_invalid_header_fields") is True
    listener = tf["resources"]["aws_lb_listener"]["https"]
    assert attr(listener, "protocol") == "HTTPS" and "TLS13" in attr(listener, "ssl_policy")
    assert "aws_lb_listener" in tf["resources"] and not [l for l in tf["resources"]["aws_lb_listener"] if l != "https"]


# ---- 6. hygiene ----------------------------------------------------------------------------------------------------------------
def test_no_hardcoded_account_ids_or_credentials():
    text = "\n".join(line for _, _, line in code_lines())
    assert not re.search(r'(?<![\w-])\d{12}(?![\w-])', text), "hardcoded 12-digit account id"
    assert "AKIA" not in text and not re.search(r'(?i)\b(secret|password|token)\s*=\s*"[^"$]{6,}"', text)


def test_state_and_variable_files_cannot_be_committed_by_accident():
    ignore = (TF_DIR / ".gitignore").read_text()
    for pattern in ("*.tfstate", "*.tfvars", "!*.tfvars.example", "backend.tf", ".terraform/"):
        assert pattern in ignore


def test_availability_zones_are_pinned_not_discovered(tf):
    """Subnets (and so the database) live in these zones; auto-discovery could silently reshuffle them."""
    assert "availability_zones" in tf["variables"] and "az_count" not in tf["variables"]
    az = tf["data"]["aws_availability_zones"]["selected"]
    assert attr(az["filter"][0], "name") == "zone-name"
    assert "postcondition" in json.dumps(az)          # a wrong zone name fails loudly at plan time
    assert "sort(" in attr(tf["locals"], "azs", "") or "sort(" in str(tf["locals"]["azs"])


def test_cloudtrail_captures_original_access_and_validates_log_files(tf):
    trail = tf["resources"]["aws_cloudtrail"]["main"]
    assert attr(trail, "enable_log_file_validation") is True and attr(trail, "enable_logging") is True
    assert attr(trail, "is_multi_region_trail") is True
    assert "aws_kms_key.main" in attr(trail, "kms_key_id")
    selector = trail["event_selector"][0]
    assert attr(selector, "include_management_events") is True
    assert attr(selector["data_resource"][0], "type") == "AWS::S3::Object"
    assert "aws_s3_bucket.documents.arn" in str(selector["data_resource"][0])
    policy = tf["data"]["aws_iam_policy_document"]["audit_bucket"]
    assert "aws:SourceArn" in json.dumps(policy)
    encryption = [s for s in statements(tf["data"]["aws_iam_policy_document"]["kms"])
                  if attr(s, "sid") == "CloudTrailEncryption"][0]
    assert "kms:EncryptionContext:aws:cloudtrail:arn" in json.dumps(encryption)


def test_access_analyzer_and_gateway_endpoint_boundaries_are_present(tf):
    assert attr(tf["resources"]["aws_accessanalyzer_analyzer"]["main"], "type") == "ACCOUNT"
    for service in ("s3", "dynamodb"):
        assert f"data.aws_iam_policy_document.{service}_endpoint" in attr(tf["resources"]["aws_vpc_endpoint"][service], "policy")
    s3 = json.dumps(tf["data"]["aws_iam_policy_document"]["s3_endpoint"])
    assert "starport-layer-bucket" in s3 and "aws:PrincipalAccount" in s3
    ddb = json.dumps(tf["data"]["aws_iam_policy_document"]["dynamodb_endpoint"])
    assert "aws_dynamodb_table.idempotency.arn" in ddb


def test_dashboard_and_cost_error_alarms_use_the_app_metrics(tf):
    dashboard = attr(tf["resources"]["aws_cloudwatch_dashboard"]["main"], "dashboard_body")
    assert "ClaimLens" in dashboard and "IF(docs>0,cost/docs,0)" in dashboard
    for name in ("processing_errors", "cost_per_document"):
        alarm = tf["resources"]["aws_cloudwatch_metric_alarm"][name]
        assert attr(alarm, "alarm_actions") == ["${aws_sns_topic.alerts.arn}"]
    assert "CLAIMLENS_METRICS" in str(tf["resources"]["aws_ecs_task_definition"]["worker"])


def test_analytics_is_opt_in_encrypted_bounded_and_not_a_worker_permission(tf):
    assert attr(tf["variables"]["enable_analytics"], "default") is False
    table = tf["resources"]["aws_glue_catalog_table"]["claims"]
    assert "exports/" in str(table) and "parquet" in str(table).lower()
    workgroup = tf["resources"]["aws_athena_workgroup"]["analytics"]
    assert "bytes_scanned_cutoff_per_query" in str(workgroup) and "SSE_KMS" in str(workgroup)
    assert "analytics" not in json.dumps(tf["data"]["aws_iam_policy_document"]["worker"])


def test_deployment_uses_versioned_migration_and_opaque_names():
    text = (TF_DIR / "ecs.tf").read_text()
    assert 'command     = ["db-migrate"]' in text and "CLAIMLENS_OPAQUE_SOURCE_NAMES" in text
