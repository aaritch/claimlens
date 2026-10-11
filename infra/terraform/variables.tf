# ---------------------------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------------------------
variable "project" {
  description = "Short name used in resource names and tags."
  type        = string
  default     = "claimlens"
}

variable "environment" {
  description = "Environment name (dev, staging, prod). One stack per environment."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "Region to deploy into. Bedrock models and Textract must be available here."
  type        = string
  default     = "us-east-1"
}

variable "allowed_account_ids" {
  description = "Optional expected AWS account IDs. Deployment configs should set this to prevent wrong-account applies."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for account_id in var.allowed_account_ids : can(regex("^[0-9]{12}$", account_id))])
    error_message = "Each allowed_account_ids entry must be a 12-digit AWS account ID."
  }
}

variable "extra_tags" {
  description = "Additional tags applied to every resource."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------------------------
# Safety switches
# ---------------------------------------------------------------------------------------------
variable "deletion_protection" {
  description = "Protect the database, DynamoDB table and load balancer from deletion. Set false only in throwaway dev stacks."
  type        = bool
  default     = true
}

variable "force_destroy" {
  description = "Allow terraform destroy to delete non-empty buckets/ECR and skip the final DB snapshot. Throwaway dev ONLY."
  type        = bool
  default     = false
}

variable "kms_deletion_window_days" {
  description = "Waiting period before a deleted KMS key is really destroyed (7-30)."
  type        = number
  default     = 30
}

variable "secret_recovery_days" {
  description = "Recovery window for deleted Secrets Manager secrets (0 = delete immediately, dev only; otherwise 7-30)."
  type        = number
  default     = 7
}

# ---------------------------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------------------------
variable "vpc_cidr" {
  description = "CIDR block for the VPC (private subnets only, no internet gateway)."
  type        = string
  default     = "10.20.0.0/16"
}

variable "availability_zones" {
  description = <<-EOT
    Availability zone NAMES to use, e.g. ["us-east-1a", "us-east-1b"]. Pinned explicitly on purpose: the subnets (and so the
    database) live in these zones, and auto-discovery could silently change the set if AWS adds or reorders a zone.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) >= 2 && length(var.availability_zones) <= 3 && length(distinct(var.availability_zones)) == length(var.availability_zones)
    error_message = "Provide 2 or 3 distinct availability zone names."
  }
}

# ---------------------------------------------------------------------------------------------
# Documents, retention, logs
# ---------------------------------------------------------------------------------------------
variable "upload_prefix" {
  description = "S3 prefix that receives uploads. Only objects under it trigger processing."
  type        = string
  default     = "incoming/"
}

variable "original_retention_days" {
  description = "Days an uploaded original is kept before S3 expires it. A POLICY decision: set it to your retention requirement."
  type        = number
  default     = 30
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days (365 default for regulated workloads; lower it in throwaway dev stacks to save cost)."
  type        = number
  default     = 365
}

# ---------------------------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------------------------
variable "queue_visibility_seconds" {
  description = "How long a received message stays hidden. The worker heartbeat extends it for long jobs."
  type        = number
  default     = 900
}

variable "max_receive_count" {
  description = "Deliveries before SQS moves a message to the dead-letter queue."
  type        = number
  default     = 5
}

variable "alarm_email" {
  description = "Email for alarm notifications (must confirm the subscription). Empty = no subscription."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------------------------
# Data stores
# ---------------------------------------------------------------------------------------------
variable "db_instance_class" {
  description = "RDS instance class. Size for your document volume; the worker, not the database, is usually the bottleneck."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_allocated_storage" {
  description = "Initial database storage in GiB (gp3)."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "Storage autoscaling ceiling in GiB."
  type        = number
  default     = 100
}

variable "db_multi_az" {
  description = "Standby replica in a second AZ for failover. Roughly doubles database cost; turn on for production."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  description = "Days of automated database backups (point-in-time recovery window)."
  type        = number
  default     = 7
}

variable "redis_node_type" {
  description = "ElastiCache node type. The cache holds only document IDs and statuses, so small is fine."
  type        = string
  default     = "cache.t4g.small"
}

variable "redis_num_nodes" {
  description = "1 = single node (dev). 2+ = replicas with automatic failover."
  type        = number
  default     = 1
}

# ---------------------------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------------------------
variable "image_tag" {
  description = "Container image tag. Use the git SHA: the ECR repo is immutable, so :latest is impossible by design."
  type        = string
  default     = ""
}

variable "enable_services" {
  description = "Create the ECS services. Apply once with false, push an image, then set true (the ECR repo must exist first)."
  type        = bool
  default     = false
}

variable "bedrock_model_id" {
  description = "Bedrock model or inference-profile ID used for extraction (enabled in your account)."
  type        = string
  default     = ""
}

variable "bedrock_classify_model_id" {
  description = "Model used for classification. Empty = same as bedrock_model_id."
  type        = string
  default     = ""
}

variable "bedrock_model_arns" {
  description = <<-EOT
    ARNs the worker may invoke: foundation-model and/or inference-profile ARNs. If you use a cross-region inference
    profile, ALSO list the underlying foundation-model ARNs in each destination region.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.bedrock_model_arns) > 0
    error_message = "List at least one Bedrock model or inference-profile ARN (least privilege: no wildcards)."
  }
}

variable "ocr_spec" {
  description = "OCR strategy: none | tesseract | textract | tesseract+textract."
  type        = string
  default     = "tesseract+textract"
}

variable "worker_cpu" {
  description = "Fargate CPU units for one worker task (1024 = 1 vCPU). Tesseract OCR is CPU-hungry; Textract-only needs far less."
  type        = number
  default     = 2048
}

variable "worker_memory" {
  description = "Fargate memory (MiB) for one worker task. Must be a valid pairing with worker_cpu."
  type        = number
  default     = 4096
}

variable "worker_min_capacity" {
  description = "Minimum worker tasks kept running (autoscaling floor)."
  type        = number
  default     = 1
}

variable "worker_max_capacity" {
  description = "Maximum worker tasks (autoscaling ceiling). This also caps how hard you can hit Bedrock and Textract rate limits."
  type        = number
  default     = 4
}

variable "worker_concurrency" {
  description = "Documents processed in parallel inside ONE worker task."
  type        = number
  default     = 4
}

variable "scale_out_backlog" {
  description = "Queue depth (visible messages) that triggers adding workers."
  type        = number
  default     = 10
}

# ---------------------------------------------------------------------------------------------
# Optional API (internal load balancer)
# ---------------------------------------------------------------------------------------------
variable "enable_api" {
  description = "Create the API service behind an INTERNAL load balancer."
  type        = bool
  default     = false
}

variable "api_certificate_arn" {
  description = "ACM certificate for the HTTPS listener. Required when enable_api = true."
  type        = string
  default     = ""
}

variable "api_allowed_cidrs" {
  description = "CIDRs allowed to reach the internal load balancer (e.g. your corporate network / VPN). No default: be explicit."
  type        = list(string)
  default     = []
}

variable "api_cpu" {
  description = "Fargate CPU units for one API task."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Fargate memory (MiB) for one API task."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = "Number of API tasks (2+ for availability across AZs)."
  type        = number
  default     = 2
}

variable "audit_retention_days" {
  description = "CloudTrail object retention. Align with the approved audit policy before production."
  type        = number
  default     = 365
}

variable "cost_per_document_alarm_usd" {
  description = "Alarm when average attempted-processing cost per document exceeds this placeholder threshold."
  type        = number
  default     = 0.05
}

variable "enable_analytics" {
  description = "Provision the optional encrypted Parquet bucket, Glue table, and Athena workgroup."
  type        = bool
  default     = false
}

variable "analytics_retention_days" {
  description = "Retention for synthetic analytics snapshots and Athena query results."
  type        = number
  default     = 30
}

variable "oidc_issuer" {
  description = "Optional HTTPS OIDC issuer. Configure reachable private JWKS networking before enabling."
  type        = string
  default     = ""
}

variable "oidc_audience" {
  description = "Audience of accepted OIDC ID tokens."
  type        = string
  default     = ""
}

variable "oidc_jwks_url" {
  description = "Pinned HTTPS JWKS URL for the OIDC issuer; must be reachable from private API tasks."
  type        = string
  default     = ""
}
