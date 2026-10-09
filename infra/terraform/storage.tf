# ---------------------------------------------------------------------------------------------
# Documents bucket: where uploads land. KMS-encrypted, private, versioned, TLS-only, access-logged.
# ---------------------------------------------------------------------------------------------
resource "aws_s3_bucket" "documents" {
  #checkov:skip=CKV_AWS_144:Single-region by design. Cross-region replication is a disaster-recovery decision to make deliberately (it copies regulated data to a second region).
  bucket        = local.documents_bucket_name
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_ownership_controls" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    bucket_key_enabled = true # fewer KMS calls = lower cost

    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
  }
}

data "aws_iam_policy_document" "documents_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.documents.arn, "${aws_s3_bucket.documents.arn}/*"]

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
}

resource "aws_s3_bucket_policy" "documents" {
  bucket     = aws_s3_bucket.documents.id
  policy     = data.aws_iam_policy_document.documents_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.documents]
}

# Originals expire (a retention POLICY: see var.original_retention_days). Textract's temporary copies expire fast.
resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket     = aws_s3_bucket.documents.id
  depends_on = [aws_s3_bucket_versioning.documents]

  rule {
    id     = "expire-uploaded-originals"
    status = "Enabled"

    filter {
      prefix = var.upload_prefix
    }

    expiration {
      days = var.original_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 7
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "abort-incomplete-uploads-everywhere"
    status = "Enabled"

    filter {
      prefix = ""
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "expire-ocr-temp"
    status = "Enabled"

    filter {
      prefix = local.ocr_temp_prefix
    }

    expiration {
      days = 1
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Turn on S3 -> EventBridge. Every object-created event now flows to EventBridge, where a rule filters by prefix.
resource "aws_s3_bucket_notification" "documents" {
  bucket      = aws_s3_bucket.documents.id
  eventbridge = true
}

resource "aws_s3_bucket_logging" "documents" {
  bucket        = aws_s3_bucket.documents.id
  target_bucket = aws_s3_bucket.access_logs.id
  target_prefix = "documents/"

  # S3 checks the log bucket's policy at the moment logging is configured, so the policy must exist first.
  depends_on = [aws_s3_bucket_policy.access_logs]
}

# ---------------------------------------------------------------------------------------------
# Access-log bucket. S3 server access logs can only be delivered to SSE-S3 (AES256) buckets, not KMS ones.
# ---------------------------------------------------------------------------------------------
resource "aws_s3_bucket" "access_logs" {
  #checkov:skip=CKV_AWS_18:This IS the log destination bucket; logging it into itself is circular.
  #checkov:skip=CKV_AWS_144:Access logs do not need cross-region replication.
  #checkov:skip=CKV_AWS_145:S3 server access log delivery does not support SSE-KMS target buckets; AES256 is the only valid option.
  #checkov:skip=CKV2_AWS_62:No event notifications are needed on the log bucket.
  bucket        = "${local.name}-access-logs-${local.account_id}-${var.aws_region}"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_ownership_controls" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  bucket                  = aws_s3_bucket.access_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "access_logs" {
  bucket = aws_s3_bucket.access_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

data "aws_iam_policy_document" "access_logs_bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.access_logs.arn, "${aws_s3_bucket.access_logs.arn}/*"]

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
    sid       = "AllowS3ServerAccessLogDelivery"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.access_logs.arn}/documents/*"]

    principals {
      type        = "Service"
      identifiers = ["logging.s3.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.documents.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_s3_bucket_policy" "access_logs" {
  bucket     = aws_s3_bucket.access_logs.id
  policy     = data.aws_iam_policy_document.access_logs_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.access_logs]
}

resource "aws_s3_bucket_lifecycle_configuration" "access_logs" {
  bucket     = aws_s3_bucket.access_logs.id
  depends_on = [aws_s3_bucket_versioning.access_logs]

  rule {
    id     = "expire-logs"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 365
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}
