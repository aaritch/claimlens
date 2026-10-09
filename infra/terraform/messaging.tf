# S3 -> EventBridge -> SQS (+ dead-letter queue) -> worker.
# Why a queue between EventBridge and the worker? A restaurant ticket rail: 500 uploads at once do not overwhelm
# Bedrock rate limits, and a ticket that keeps failing lands in a "problem orders" spot (the DLQ) instead of
# vanishing.

resource "aws_sqs_queue" "dlq" {
  name                              = "${local.name}-documents-dlq"
  message_retention_seconds         = 1209600 # 14 days to investigate
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300
}

resource "aws_sqs_queue" "main" {
  name                              = "${local.name}-documents"
  visibility_timeout_seconds        = var.queue_visibility_seconds
  message_retention_seconds         = 345600 # 4 days
  receive_wait_time_seconds         = 20     # long polling
  kms_master_key_id                 = aws_kms_key.main.arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.main.arn]
  })
}

resource "aws_cloudwatch_event_rule" "uploads" {
  name        = "${local.name}-document-uploaded"
  description = "New object under ${var.upload_prefix} in the documents bucket"

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [aws_s3_bucket.documents.id] }
      object = { key = [{ prefix = var.upload_prefix }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "queue" {
  rule      = aws_cloudwatch_event_rule.uploads.name
  target_id = "documents-queue"
  arn       = aws_sqs_queue.main.arn

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }
}

data "aws_iam_policy_document" "queue" {
  statement {
    sid       = "AllowOnlyOurEventBridgeRule"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.main.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.uploads.arn]
    }
  }

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["sqs:*"]
    resources = [aws_sqs_queue.main.arn]

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

resource "aws_sqs_queue_policy" "main" {
  queue_url = aws_sqs_queue.main.id
  policy    = data.aws_iam_policy_document.queue.json
}

# ---------------------------------------------------------------------------------------------
# Alerts. The ones that mean "documents are NOT flowing" must never be silent.
# ---------------------------------------------------------------------------------------------
resource "aws_sns_topic" "alerts" {
  name              = "${local.name}-alerts"
  kms_master_key_id = aws_kms_key.main.arn
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${local.name}-dlq-not-empty"
  alarm_description   = "A document failed ${var.max_receive_count} times and landed in the dead-letter queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "queue_stalled" {
  alarm_name          = "${local.name}-queue-oldest-message-old"
  alarm_description   = "The oldest message has waited 30+ minutes: workers are down, stuck or under-provisioned."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.main.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1800
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "events_failing" {
  alarm_name          = "${local.name}-eventbridge-delivery-failing"
  alarm_description   = "EventBridge could not deliver upload events to the queue (check the KMS key policy and queue policy)."
  namespace           = "AWS/Events"
  metric_name         = "FailedInvocations"
  dimensions          = { RuleName = aws_cloudwatch_event_rule.uploads.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}
