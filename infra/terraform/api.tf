# Optional API behind an INTERNAL load balancer (reachable from your network/VPN, not the internet).
# To expose it publicly later, put API Gateway / CloudFront + WAF in front. Do not just flip the load balancer to public.
resource "aws_security_group" "alb" {
  count = var.enable_api ? 1 : 0

  name_prefix = "${local.name}-alb-"
  description = "Internal API load balancer"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-alb" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  count = var.enable_api ? length(var.api_allowed_cidrs) : 0

  security_group_id = aws_security_group.alb[0].id
  description       = "HTTPS from an allowed network"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.api_allowed_cidrs[count.index]
}

resource "aws_vpc_security_group_egress_rule" "alb_to_app" {
  count = var.enable_api ? 1 : 0

  security_group_id            = aws_security_group.alb[0].id
  description                  = "To API tasks"
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  referenced_security_group_id = aws_security_group.app.id
}

resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {
  count = var.enable_api ? 1 : 0

  security_group_id            = aws_security_group.app.id
  description                  = "API traffic from the load balancer"
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
  referenced_security_group_id = aws_security_group.alb[0].id
}

resource "aws_lb" "api" {
  count = var.enable_api ? 1 : 0

  name                       = "${local.name}-api"
  internal                   = true
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb[0].id]
  subnets                    = aws_subnet.private[*].id
  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection
}

resource "aws_lb_target_group" "api" {
  count = var.enable_api ? 1 : 0

  name        = "${local.name}-api"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  count = var.enable_api ? 1 : 0

  load_balancer_arn = aws_lb.api[0].arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.api_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api[0].arn
  }

  lifecycle {
    precondition {
      condition     = var.api_certificate_arn != ""
      error_message = "enable_api requires api_certificate_arn (an ACM certificate): the API is HTTPS-only."
    }
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([merge(local.hardening, {
    name      = "api"
    image     = local.image
    essential = true
    command   = ["serve", "--host", "0.0.0.0", "--port", "8080"]
    portMappings = [{
      containerPort = 8080
      protocol      = "tcp"
    }]
    environment = concat(local.common_environment, local.runtime_environment, [
      { name = "CLAIMLENS_SYNC_UPLOAD", value = "0" }, # production uploads go browser -> S3 -> queue -> worker
      { name = "CLAIMLENS_API_DOCS", value = "0" },
    ])
    secrets = concat(local.db_secrets, local.redis_secrets, [
      { name = "CLAIMLENS_API_TOKENS", valueFrom = aws_secretsmanager_secret.api_tokens.arn },
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "api"
      }
    }
  })])
}

resource "aws_ecs_service" "api" {
  count = var.enable_api && var.enable_services ? 1 : 0

  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api[0].arn
    container_name   = "api"
    container_port   = 8080
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.https, aws_vpc_endpoint.interface]
}
