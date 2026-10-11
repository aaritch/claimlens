# ---------------------------------------------------------------------------------------------
# DynamoDB: the idempotency "coat-check ticket" table (see claimlens/storage/idempotency.py).
# ---------------------------------------------------------------------------------------------
resource "aws_dynamodb_table" "idempotency" {
  name                        = "${local.name}-idempotency"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  deletion_protection_enabled = var.deletion_protection

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at" # records vanish after 30 days
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.main.arn
  }
}

# ---------------------------------------------------------------------------------------------
# PostgreSQL (RDS): the system of record.
# ---------------------------------------------------------------------------------------------
resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "database" {
  name_prefix = "${local.name}-db-"
  description = "PostgreSQL: reachable only from the application tasks"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-db" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "database_from_app" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from application tasks"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app.id
}

resource "aws_db_parameter_group" "postgres" {
  name_prefix = "${local.name}-pg16-"
  family      = "postgres16"
  description = "Require TLS and log connections"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot" # Match PostgreSQL 16's enabled system default to avoid perpetual drift.
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# RDS enhanced monitoring: OS-level metrics every 60s. Needs a role RDS can assume.
data "aws_iam_policy_document" "rds_monitoring_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name               = "${local.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.rds_monitoring_assume.json
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_db_instance" "main" {
  #checkov:skip=CKV_AWS_157:Multi-AZ is a cost decision controlled by var.db_multi_az (off for dev, turn on for production).
  identifier     = "${local.name}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  db_name  = "claimlens"
  username = "claimlens_admin"

  # RDS creates and rotates the master password in Secrets Manager. It never appears in Terraform code or tfvars.
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.main.arn

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.main.arn

  multi_az               = var.db_multi_az
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.postgres.name

  backup_retention_period    = var.db_backup_retention_days
  copy_tags_to_snapshot      = true
  deletion_protection        = var.deletion_protection
  skip_final_snapshot        = var.force_destroy
  final_snapshot_identifier  = var.force_destroy ? null : "${local.name}-db-final"
  auto_minor_version_upgrade = true

  iam_database_authentication_enabled = true
  monitoring_interval                 = 60
  monitoring_role_arn                 = aws_iam_role.rds_monitoring.arn
  performance_insights_enabled        = true
  performance_insights_kms_key_id     = aws_kms_key.main.arn
  enabled_cloudwatch_logs_exports     = ["postgresql", "upgrade"]

  lifecycle {
    ignore_changes = [engine_version] # minor versions upgrade automatically
  }
}

# ---------------------------------------------------------------------------------------------
# Redis (ElastiCache): the duplicate fast-path. Holds only {document_id, status}: never extracted values.
# ---------------------------------------------------------------------------------------------
resource "random_password" "redis_auth" {
  length  = 48
  special = false # ElastiCache restricts the auth-token alphabet; alphanumerics are plenty at this length
}

resource "aws_secretsmanager_secret" "redis_auth" {
  #checkov:skip=CKV2_AWS_57:Rotation needs a rotation function and a rollout plan for every consumer. It is an OPERATIONAL TODO (see README), not something to fake with a half-configured Lambda.
  name                    = "${local.name}/redis-auth-token"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = var.secret_recovery_days
}

resource "aws_secretsmanager_secret_version" "redis_auth" {
  secret_id     = aws_secretsmanager_secret.redis_auth.id
  secret_string = random_password.redis_auth.result
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name}-cache"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_security_group" "redis" {
  name_prefix = "${local.name}-redis-"
  description = "Redis: reachable only from the application tasks"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-redis" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_app" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Redis from application tasks"
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.app.id
}

resource "aws_elasticache_replication_group" "main" {
  #checkov:skip=CKV2_AWS_50:Automatic failover/multi-AZ follows var.redis_num_nodes (1 in dev; use 2+ in production). The cache is a speed-up only: losing it costs latency, never correctness.
  replication_group_id = "${local.name}-cache"
  description          = "ClaimLens duplicate fast-path cache"

  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  port                 = 6379
  parameter_group_name = "default.redis7"

  num_cache_clusters         = var.redis_num_nodes
  automatic_failover_enabled = var.redis_num_nodes > 1
  multi_az_enabled           = var.redis_num_nodes > 1

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.main.arn
  transit_encryption_enabled = true
  auth_token                 = random_password.redis_auth.result
}

# ---------------------------------------------------------------------------------------------
# API tokens: the secret is created EMPTY. Set its value out-of-band so it never touches Terraform state:
#   aws secretsmanager put-secret-value --secret-id <arn> --secret-string 'token1:rita:reader,token2:rob:reviewer'
# ---------------------------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "api_tokens" {
  #checkov:skip=CKV2_AWS_57:Rotation needs a rotation function and a rollout plan for every consumer. It is an OPERATIONAL TODO (see README), not something to fake with a half-configured Lambda.
  name                    = "${local.name}/api-tokens"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = var.secret_recovery_days
}
