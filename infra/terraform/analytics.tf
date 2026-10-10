# Opt-in only. Exported snapshots must contain synthetic business fields, not originals or raw model output.
resource "aws_s3_bucket" "analytics" {
  count         = var.enable_analytics ? 1 : 0
  bucket        = "${local.name}-analytics-${local.account_id}-${var.aws_region}"
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_ownership_controls" "analytics" {
  count  = var.enable_analytics ? 1 : 0
  bucket = aws_s3_bucket.analytics[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "analytics" {
  count                   = var.enable_analytics ? 1 : 0
  bucket                  = aws_s3_bucket.analytics[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "analytics" {
  count  = var.enable_analytics ? 1 : 0
  bucket = aws_s3_bucket.analytics[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "analytics" {
  count  = var.enable_analytics ? 1 : 0
  bucket = aws_s3_bucket.analytics[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
  }
}

data "aws_iam_policy_document" "analytics_bucket" {
  count = var.enable_analytics ? 1 : 0
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.analytics[0].arn, "${aws_s3_bucket.analytics[0].arn}/*"]
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

resource "aws_s3_bucket_policy" "analytics" {
  count      = var.enable_analytics ? 1 : 0
  bucket     = aws_s3_bucket.analytics[0].id
  policy     = data.aws_iam_policy_document.analytics_bucket[0].json
  depends_on = [aws_s3_bucket_public_access_block.analytics]
}

resource "aws_s3_bucket_lifecycle_configuration" "analytics" {
  count  = var.enable_analytics ? 1 : 0
  bucket = aws_s3_bucket.analytics[0].id
  rule {
    id     = "expire-snapshots"
    status = "Enabled"
    filter {
      prefix = ""
    }
    expiration {
      days = var.analytics_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_glue_catalog_database" "analytics" {
  count = var.enable_analytics ? 1 : 0
  name  = replace("${local.name}_analytics", "-", "_")
}

resource "aws_glue_catalog_table" "claims" {
  count         = var.enable_analytics ? 1 : 0
  name          = "validated_claims"
  database_name = aws_glue_catalog_database.analytics[0].name
  table_type    = "EXTERNAL_TABLE"
  parameters    = { classification = "parquet", EXTERNAL = "TRUE" }
  storage_descriptor {
    location      = "s3://${aws_s3_bucket.analytics[0].id}/exports/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"
    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }
    dynamic "columns" {
      for_each = {
        id            = "string", doc_type = "string", payer_name = "string", provider_npi = "string",
        service_date  = "date", disputed_amount = "decimal(12,2)", input_tokens = "bigint",
        output_tokens = "bigint", ocr_pages = "bigint", cost_usd = "decimal(10,6)",
        elapsed_ms    = "bigint", version = "bigint", updated_at = "timestamp",
      }
      content {
        name = columns.key
        type = columns.value
      }
    }
  }
}

resource "aws_athena_workgroup" "analytics" {
  count         = var.enable_analytics ? 1 : 0
  name          = "${local.name}-analytics"
  force_destroy = var.force_destroy
  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = 1073741824
    result_configuration {
      output_location = "s3://${aws_s3_bucket.analytics[0].id}/query-results/"
      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = aws_kms_key.main.arn
      }
    }
  }
}
