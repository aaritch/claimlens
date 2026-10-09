# ---------------------------------------------------------------------------------------------
# Three principals, each with only what it needs:
#   execution role : ECS itself (pull the image, write logs, inject secrets). Never seen by our code.
#   worker task role: what the worker's CODE may do (read uploads, call Bedrock/Textract, write idempotency records).
#   api task role   : what the API's code may do (mint upload tickets). It cannot read documents or call Bedrock.
# ---------------------------------------------------------------------------------------------
data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

# ---- execution role ------------------------------------------------------------------------------
data "aws_iam_policy_document" "execution" {
  statement {
    sid       = "EcrAuthToken"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # AWS does not support resource-level permissions for this action
  }

  statement {
    sid = "PullOurImageOnly"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid     = "WriteOurLogs"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.worker.arn}:*",
      "${aws_cloudwatch_log_group.api.arn}:*",
      "${aws_cloudwatch_log_group.migrate.arn}:*",
    ]
  }

  statement {
    sid     = "ReadOnlyTheSecretsWeInject"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_db_instance.main.master_user_secret[0].secret_arn,
      aws_secretsmanager_secret.redis_auth.arn,
      aws_secretsmanager_secret.api_tokens.arn,
    ]
  }

  statement {
    sid       = "DecryptSecretsAndImages"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "execution" {
  name   = "execution"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ---- worker task role ---------------------------------------------------------------------------
data "aws_iam_policy_document" "worker" {
  statement {
    sid = "ConsumeTheWorkQueue"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:ChangeMessageVisibility",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.main.arn]
  }

  statement {
    sid       = "ReadAndTagUploadedDocuments"
    actions   = ["s3:GetObject", "s3:PutObjectTagging"]
    resources = ["${aws_s3_bucket.documents.arn}/${var.upload_prefix}*"]
  }

  # Without s3:ListBucket, S3 answers "403 Access Denied" (not "404 Not Found") for a missing key, which would make a
  # deleted-by-lifecycle upload look like a permissions bug. The bucket only holds our two prefixes anyway.
  statement {
    sid       = "SeeWhetherAnObjectExists"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn]
  }

  # Async Textract reads the PDF AS THIS ROLE, so the temp copy must be readable by it.
  statement {
    sid       = "TextractTemporaryCopies"
    actions   = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.documents.arn}/${local.ocr_temp_prefix}*"]
  }

  statement {
    sid       = "UseTheDataKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.main.arn]
  }

  statement {
    sid       = "IdempotencyRecords"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.idempotency.arn]
  }

  statement {
    sid       = "InvokeApprovedModelsOnly"
    actions   = ["bedrock:InvokeModel"]
    resources = var.bedrock_model_arns
  }

  statement {
    sid = "Textract"
    actions = [
      "textract:DetectDocumentText",
      "textract:StartDocumentTextDetection",
      "textract:GetDocumentTextDetection",
    ]
    resources = ["*"] # Textract does not support resource-level permissions
  }
}

resource "aws_iam_role" "worker_task" {
  name               = "${local.name}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "worker_task" {
  name   = "worker"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker.json
}

# ---- API task role ------------------------------------------------------------------------------
data "aws_iam_policy_document" "api" {
  statement {
    sid       = "SignUploadTickets"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/${var.upload_prefix}*"]
  }

  statement {
    sid       = "UseTheDataKeyForSignedUploads"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_role" "api_task" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy" "api_task" {
  name   = "api"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api.json
}

# ---- migration task role: needs NO AWS permissions (it only talks to the database) ------------------
resource "aws_iam_role" "migrate_task" {
  name               = "${local.name}-migrate-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}
