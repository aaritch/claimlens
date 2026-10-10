# Audit trails include S3 object data events, not just management events.
resource "aws_s3_bucket" "audit" {
  bucket        = "${local.name}-audit-${local.account_id}-${var.aws_region}"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_ownership_controls" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    id     = "retain-audit"
    status = "Enabled"
    filter {
      prefix = ""
    }
    expiration {
      days = var.audit_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.audit_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

locals {
  trail_arn = "arn:aws:cloudtrail:${var.aws_region}:${local.account_id}:trail/${local.name}"
}

data "aws_iam_policy_document" "audit_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
  statement {
    sid       = "CloudTrailAclCheck"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.audit.arn]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }
  statement {
    sid       = "CloudTrailDelivery"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.audit.arn}/AWSLogs/${local.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }
}

resource "aws_s3_bucket_policy" "audit" {
  bucket     = aws_s3_bucket.audit.id
  policy     = data.aws_iam_policy_document.audit_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.audit]
}

resource "aws_cloudtrail" "main" {
  name                          = local.name
  s3_bucket_name                = aws_s3_bucket.audit.id
  kms_key_id                    = aws_kms_key.main.arn
  enable_logging                = true
  enable_log_file_validation    = true
  is_multi_region_trail         = true
  include_global_service_events = true

  event_selector {
    include_management_events = true
    read_write_type           = "All"
    data_resource {
      type   = "AWS::S3::Object"
      values = ["${aws_s3_bucket.documents.arn}/"]
    }
  }
  depends_on = [aws_s3_bucket_policy.audit]
}

resource "aws_accessanalyzer_analyzer" "main" {
  analyzer_name = local.name
  type          = "ACCOUNT"
}

# Gateway endpoint policies are another boundary, not a substitute for task IAM.
# ECR's regional layer bucket must remain readable or Fargate cannot pull images without NAT.
data "aws_iam_policy_document" "s3_endpoint" {
  statement {
    actions = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:PutObjectTagging", "s3:ListBucket"]
    resources = concat(
      [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"],
      var.enable_analytics ? [aws_s3_bucket.analytics[0].arn, "${aws_s3_bucket.analytics[0].arn}/*"] : []
    )
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [local.account_id]
    }
  }
  statement {
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::prod-${var.aws_region}-starport-layer-bucket/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

data "aws_iam_policy_document" "dynamodb_endpoint" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.idempotency.arn]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [local.account_id]
    }
  }
}
