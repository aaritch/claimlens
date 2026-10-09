resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/${var.project}/${var.environment}/worker"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/${var.project}/${var.environment}/api"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_cloudwatch_log_group" "migrate" {
  name              = "/${var.project}/${var.environment}/migrate"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

locals {
  image = "${aws_ecr_repository.app.repository_url}:${coalesce(var.image_tag, "unset")}"

  # Non-secret settings every container shares. The app assembles its database and Redis URLs from these parts.
  common_environment = [
    { name = "AWS_REGION", value = var.aws_region },
    { name = "PYTHONDONTWRITEBYTECODE", value = "1" },
    { name = "CLAIMLENS_DB_HOST", value = aws_db_instance.main.address },
    { name = "CLAIMLENS_DB_PORT", value = tostring(aws_db_instance.main.port) },
    { name = "CLAIMLENS_DB_NAME", value = aws_db_instance.main.db_name },
    { name = "CLAIMLENS_DB_SSLMODE", value = "require" },
  ]

  # Injected by ECS from Secrets Manager at start time. They never appear in the task definition or in Terraform.
  db_secrets = [
    { name = "CLAIMLENS_DB_USER", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:username::" },
    { name = "CLAIMLENS_DB_PASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" },
  ]

  runtime_environment = [
    { name = "CLAIMLENS_REDIS_HOST", value = aws_elasticache_replication_group.main.primary_endpoint_address },
    { name = "CLAIMLENS_REDIS_TLS", value = "1" },
    { name = "CLAIMLENS_IDEMPOTENCY", value = "dynamodb" },
    { name = "CLAIMLENS_DYNAMODB_TABLE", value = aws_dynamodb_table.idempotency.name },
    { name = "CLAIMLENS_DOCUMENTS_BUCKET", value = aws_s3_bucket.documents.id },
    { name = "CLAIMLENS_UPLOAD_PREFIX", value = var.upload_prefix },
    { name = "CLAIMLENS_QUEUE_VISIBILITY_SECONDS", value = tostring(var.queue_visibility_seconds) },
  ]

  redis_secrets = [
    { name = "CLAIMLENS_REDIS_AUTH_TOKEN", valueFrom = aws_secretsmanager_secret.redis_auth.arn },
  ]

  # Hardening shared by every container: unprivileged user, read-only root filesystem, writable /tmp only.
  hardening = {
    user                   = "10001"
    readonlyRootFilesystem = true
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    linuxParameters        = { initProcessEnabled = true }
    healthCheck = {
      command     = ["CMD", "python", "-m", "claimlens", "healthcheck"]
      interval    = 30
      timeout     = 10
      retries     = 3
      startPeriod = 60
    }
  }
}

locals {
  # The one-off migration task has no heartbeat file or HTTP port, so the shared health check would mark it unhealthy.
  hardening_without_healthcheck = { for k, v in local.hardening : k => v if k != "healthCheck" }
}

# ---- worker ---------------------------------------------------------------------------------------
resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([merge(local.hardening, {
    name        = "worker"
    image       = local.image
    essential   = true
    command     = ["worker"]
    stopTimeout = 120 # Fargate maximum: time to finish in-flight documents after SIGTERM
    environment = concat(local.common_environment, local.runtime_environment, [
      { name = "CLAIMLENS_LLM", value = "bedrock" },
      { name = "CLAIMLENS_MODEL_ID", value = var.bedrock_model_id },
      { name = "CLAIMLENS_CLASSIFY_MODEL_ID", value = var.bedrock_classify_model_id },
      { name = "CLAIMLENS_OCR", value = var.ocr_spec },
      { name = "CLAIMLENS_TEXTRACT_BUCKET", value = aws_s3_bucket.documents.id },
      { name = "CLAIMLENS_SQS_QUEUE_URL", value = aws_sqs_queue.main.url },
      { name = "CLAIMLENS_WORKER_CONCURRENCY", value = tostring(var.worker_concurrency) },
    ])
    secrets = concat(local.db_secrets, local.redis_secrets)
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "worker"
      }
    }
  })])
}

resource "aws_ecs_service" "worker" {
  count = var.enable_services ? 1 : 0

  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_min_capacity
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true # a bad image rolls itself back instead of crash-looping
  }

  lifecycle {
    ignore_changes = [desired_count] # autoscaling owns the count after creation

    precondition {
      condition     = var.image_tag != "" && var.bedrock_model_id != ""
      error_message = "Set image_tag and bedrock_model_id before enabling services."
    }
  }

  depends_on = [aws_vpc_endpoint.interface, aws_vpc_endpoint.s3, aws_vpc_endpoint.dynamodb]
}

# ---- autoscaling on QUEUE DEPTH (CPU is the wrong signal: workers mostly wait on Bedrock/Textract) --------
resource "aws_appautoscaling_target" "worker" {
  count = var.enable_services ? 1 : 0

  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker[0].name}"
  min_capacity       = var.worker_min_capacity
  max_capacity       = var.worker_max_capacity
}

resource "aws_appautoscaling_policy" "scale_out" {
  count = var.enable_services ? 1 : 0

  name               = "${local.name}-scale-out"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  resource_id        = aws_appautoscaling_target.worker[0].resource_id

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 120
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_lower_bound = 0
      scaling_adjustment          = 2
    }
  }
}

resource "aws_appautoscaling_policy" "scale_in" {
  count = var.enable_services ? 1 : 0

  name               = "${local.name}-scale-in"
  policy_type        = "StepScaling"
  service_namespace  = aws_appautoscaling_target.worker[0].service_namespace
  scalable_dimension = aws_appautoscaling_target.worker[0].scalable_dimension
  resource_id        = aws_appautoscaling_target.worker[0].resource_id

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 300
    metric_aggregation_type = "Maximum"

    step_adjustment {
      metric_interval_upper_bound = 0
      scaling_adjustment          = -1
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "backlog_high" {
  count = var.enable_services ? 1 : 0

  alarm_name          = "${local.name}-backlog-high"
  alarm_description   = "Messages are waiting: add workers."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.main.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = var.scale_out_backlog
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions       = [aws_appautoscaling_policy.scale_out[0].arn]
}

# Scale in only when NOTHING is waiting AND nothing is being processed (visible + in-flight), for 15 minutes.
resource "aws_cloudwatch_metric_alarm" "backlog_empty" {
  count = var.enable_services ? 1 : 0

  alarm_name          = "${local.name}-backlog-empty"
  alarm_description   = "Queue idle for 15 minutes: remove a worker."
  comparison_operator = "LessThanOrEqualToThreshold"
  threshold           = 0
  evaluation_periods  = 15
  alarm_actions       = [aws_appautoscaling_policy.scale_in[0].arn]

  metric_query {
    id = "visible"

    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesVisible"
      dimensions  = { QueueName = aws_sqs_queue.main.name }
      period      = 60
      stat        = "Maximum"
    }
  }

  metric_query {
    id = "inflight"

    metric {
      namespace   = "AWS/SQS"
      metric_name = "ApproximateNumberOfMessagesNotVisible"
      dimensions  = { QueueName = aws_sqs_queue.main.name }
      period      = 60
      stat        = "Maximum"
    }
  }

  metric_query {
    id          = "total"
    expression  = "visible + inflight"
    label       = "Messages waiting or in progress"
    return_data = true
  }
}

# ---- one-off database migration task (run it with the command in the outputs) ------------------------
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.migrate_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([merge(local.hardening_without_healthcheck, {
    name        = "migrate"
    image       = local.image
    essential   = true
    command     = ["db-init"]
    environment = local.common_environment
    secrets     = local.db_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.migrate.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "migrate"
      }
    }
  })])
}
