output "documents_bucket" {
  value = aws_s3_bucket.documents.id
}

output "queue_url" {
  value = aws_sqs_queue.main.url
}

output "dead_letter_queue_url" {
  description = "Messages here failed repeatedly. Investigate, fix, then redrive from the SQS console."
  value       = aws_sqs_queue.dlq.url
}

output "ecr_repository_url" {
  description = "Push images here, tagged with the git SHA."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "database_endpoint" {
  value = aws_db_instance.main.address
}

output "database_secret_arn" {
  description = "RDS-managed secret holding the database username and password."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "api_tokens_secret_arn" {
  description = "Created empty. Set it with: aws secretsmanager put-secret-value --secret-id <this> --secret-string 'token:user:role,...'"
  value       = aws_secretsmanager_secret.api_tokens.arn
}

output "api_url" {
  value = var.enable_api ? "https://${aws_lb.api[0].dns_name}" : null
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "migrate_command" {
  description = "Run ONCE after the first apply (and after schema changes) to create the database tables."
  value = format(
    "aws ecs run-task --cluster %s --launch-type FARGATE --task-definition %s --network-configuration 'awsvpcConfiguration={subnets=[%s],securityGroups=[%s],assignPublicIp=DISABLED}'",
    aws_ecs_cluster.main.name,
    aws_ecs_task_definition.migrate.family,
    join(",", aws_subnet.private[*].id),
    aws_security_group.app.id,
  )
}
