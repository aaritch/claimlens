# A VPC with NO internet gateway and NO NAT. Everything the workers need (S3, DynamoDB, ECR, Bedrock, Textract, SQS,
# KMS, Secrets Manager, STS, CloudWatch Logs) is reached through VPC endpoints, so document traffic never crosses the
# public internet. The earlier Gemini draft only created a Bedrock endpoint: a Lambda/task in private subnets could
# not have reached S3, DynamoDB, Textract or KMS.
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name}-vpc" }
}

# The default security group allows nothing. We never use it.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
}

resource "aws_subnet" "private" {
  count = length(local.azs)

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = length(local.azs)

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# ---- gateway endpoints (free) ------------------------------------------------------------------------
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name}-s3" }
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]

  tags = { Name = "${local.name}-dynamodb" }
}

# ---- interface endpoints (hourly + per-GB cost) --------------------------------------------------------
locals {
  interface_endpoints = toset([
    "ecr.api",
    "ecr.dkr",
    "logs",
    "sqs",
    "kms",
    "sts",
    "secretsmanager",
    "bedrock-runtime",
    "textract",
  ])
}

resource "aws_security_group" "endpoints" {
  name_prefix = "${local.name}-endpoints-"
  description = "Interface VPC endpoints: HTTPS from inside the VPC only"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-endpoints" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  security_group_id = aws_security_group.endpoints.id
  description       = "HTTPS from within the VPC"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.vpc_cidr
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-${each.key}" }
}

# ---- flow logs ----------------------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "flow" {
  name              = "/${var.project}/${var.environment}/vpc-flow"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

data "aws_iam_policy_document" "flow_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "flow_write" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flow.arn}:*"]
  }
}

resource "aws_iam_role" "flow" {
  name               = "${local.name}-vpc-flow"
  assume_role_policy = data.aws_iam_policy_document.flow_assume.json
}

resource "aws_iam_role_policy" "flow" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow.id
  policy = data.aws_iam_policy_document.flow_write.json
}

resource "aws_flow_log" "main" {
  vpc_id               = aws_vpc.main.id
  traffic_type         = "ALL"
  log_destination_type = "cloud-watch-logs"
  log_destination      = aws_cloudwatch_log_group.flow.arn
  iam_role_arn         = aws_iam_role.flow.arn
}

# ---- security group shared by every ECS task (worker, API, migration) -------------------------------------
# Egress is allow-listed by DESTINATION (endpoints, S3/DynamoDB prefix lists, database, cache). There is no
# 0.0.0.0/0 anywhere in this stack.
resource "aws_security_group" "app" {
  name_prefix = "${local.name}-app-"
  description = "ECS tasks (worker, API, migrations)"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-app" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "app_to_endpoints" {
  security_group_id            = aws_security_group.app.id
  description                  = "HTTPS to interface VPC endpoints"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.endpoints.id
}

resource "aws_vpc_security_group_egress_rule" "app_to_s3" {
  security_group_id = aws_security_group.app.id
  description       = "HTTPS to S3 (gateway endpoint prefix list)"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
}

resource "aws_vpc_security_group_egress_rule" "app_to_dynamodb" {
  security_group_id = aws_security_group.app.id
  description       = "HTTPS to DynamoDB (gateway endpoint prefix list)"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = aws_vpc_endpoint.dynamodb.prefix_list_id
}

resource "aws_vpc_security_group_egress_rule" "app_to_database" {
  security_group_id            = aws_security_group.app.id
  description                  = "PostgreSQL"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.database.id
}

resource "aws_vpc_security_group_egress_rule" "app_to_redis" {
  security_group_id            = aws_security_group.app.id
  description                  = "Redis (TLS)"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.redis.id
}
