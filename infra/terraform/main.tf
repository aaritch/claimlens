data "aws_caller_identity" "current" {}

# Looks up ONLY the zones we named, which proves they exist in this region and pins the set.
data "aws_availability_zones" "selected" {
  state = "available"

  filter {
    name   = "zone-name"
    values = var.availability_zones
  }

  lifecycle {
    postcondition {
      condition     = length(self.names) == length(var.availability_zones)
      error_message = "One or more availability_zones do not exist (or are unavailable) in ${var.aws_region}."
    }
  }
}

locals {
  name       = "${var.project}-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  azs        = sort(data.aws_availability_zones.selected.names) # sorted: subnet <-> AZ pairing never reshuffles

  tags = merge(
    {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
    },
    var.extra_tags,
  )

  documents_bucket_name = "${local.name}-documents-${local.account_id}-${var.aws_region}"
  ocr_temp_prefix       = "ocr-tmp/"
}
