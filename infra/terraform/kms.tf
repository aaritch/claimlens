# One customer-managed key encrypts everything that holds or touches documents: S3, SQS, SNS, DynamoDB, RDS,
# ElastiCache, ECR, Secrets Manager and CloudWatch Logs. (Per-service keys are a valid hardening step; one key keeps
# the first deployment understandable.)
data "aws_iam_policy_document" "kms" {
  #checkov:skip=CKV_AWS_109:A KMS key policy must let the account root delegate to IAM, otherwise the key becomes unmanageable. This is the standard AWS default statement.
  #checkov:skip=CKV_AWS_111:Same: the root-delegation statement is required for a manageable key.
  #checkov:skip=CKV_AWS_356:Same: in a KEY policy "*" means "this key", not "all resources".
  statement {
    sid       = "EnableIamPermissions"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid = "CloudWatchLogsEncryption"
    actions = [
      "kms:Encrypt*",
      "kms:Decrypt*",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:Describe*",
    ]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["logs.${var.aws_region}.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:*"]
    }
  }

  # EventBridge writes into the CMK-encrypted queue; CloudWatch alarms publish to the CMK-encrypted topic.
  # kms:ViaService limits use of the key to those two services.
  # FIRST-DEPLOY CHECK: if upload events never reach the queue, look at the EventBridge FailedInvocations metric.
  statement {
    sid       = "EventBridgeToEncryptedQueue"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["sqs.${var.aws_region}.amazonaws.com"]
    }
  }

  statement {
    sid       = "CloudWatchAlarmsToEncryptedTopic"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["sns.${var.aws_region}.amazonaws.com"]
    }
  }
  statement {
    sid       = "CloudTrailEncryption"
    actions   = ["kms:GenerateDataKey*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudtrail:${var.aws_region}:${local.account_id}:trail/${local.name}"]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:aws:cloudtrail:arn"
      values   = ["arn:aws:cloudtrail:${var.aws_region}:${local.account_id}:trail/${local.name}"]
    }
  }
  statement {
    sid       = "CloudTrailDescribeKey"
    actions   = ["kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = ["arn:aws:cloudtrail:${var.aws_region}:${local.account_id}:trail/${local.name}"]
    }
  }
}

resource "aws_kms_key" "main" {
  description             = "${local.name} data encryption key"
  enable_key_rotation     = true
  deletion_window_in_days = var.kms_deletion_window_days
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.main.key_id
}
