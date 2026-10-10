resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title = "Documents, review routing, and processing errors", region = var.aws_region, stat = "Sum", period = 300
          metrics = [
            ["ClaimLens", "Documents", "Environment", var.environment],
            [".", "ValidationFailures", ".", "."],
            [".", "ProcessingErrors", ".", "."],
            [".", "OCRDocuments", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title = "Tokens and OCR pages", region = var.aws_region, stat = "Sum", period = 300
          metrics = [
            ["ClaimLens", "InputTokens", "Environment", var.environment],
            [".", "OutputTokens", ".", "."],
            [".", "OCRPages", ".", "."],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title = "Processing latency (p50/p95)", region = var.aws_region, period = 300
          metrics = [
            ["ClaimLens", "Latency", "Environment", var.environment, { stat = "p50" }],
            [".", ".", ".", ".", { stat = "p95" }],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title = "Estimated cost per attempted document", region = var.aws_region, period = 300
          metrics = [
            ["ClaimLens", "CostUSD", "Environment", var.environment, { id = "cost", stat = "Sum", visible = false }],
            [".", "Documents", ".", ".", { id = "docs", stat = "Sum", visible = false }],
            [{ expression = "IF(docs>0,cost/docs,0)", id = "average", label = "USD / attempted document" }],
          ]
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 24, height = 6
        properties = {
          title = "Work queue and dead letters", region = var.aws_region, stat = "Maximum", period = 60
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.main.name],
            [".", "ApproximateNumberOfMessagesNotVisible", ".", "."],
            [".", "ApproximateNumberOfMessagesVisible", ".", aws_sqs_queue.dlq.name],
          ]
        }
      },
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "processing_errors" {
  alarm_name          = "${local.name}-processing-errors"
  alarm_description   = "The processing pipeline is failing. Check safe worker events and queue retries."
  namespace           = "ClaimLens"
  metric_name         = "ProcessingErrors"
  dimensions          = { Environment = var.environment }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "cost_per_document" {
  alarm_name          = "${local.name}-cost-per-document-high"
  alarm_description   = "Estimated processing cost exceeded the budget threshold. Verify configured model prices."
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.cost_per_document_alarm_usd
  evaluation_periods  = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  metric_query {
    id = "cost"
    metric {
      namespace   = "ClaimLens"
      metric_name = "CostUSD"
      dimensions  = { Environment = var.environment }
      period      = 300
      stat        = "Sum"
    }
  }
  metric_query {
    id = "docs"
    metric {
      namespace   = "ClaimLens"
      metric_name = "Documents"
      dimensions  = { Environment = var.environment }
      period      = 300
      stat        = "Sum"
    }
  }
  metric_query {
    id          = "average"
    expression  = "IF(docs>0,cost/docs,0)"
    label       = "Estimated USD per attempted document"
    return_data = true
  }
}
