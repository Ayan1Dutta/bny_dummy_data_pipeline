output "landing_bucket" {
  value = aws_s3_bucket.landing.bucket
}

output "lakehouse_bucket" {
  value = aws_s3_bucket.lakehouse.bucket
}

output "api_endpoint" {
  value = "https://${aws_lb.api.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "warehouse_endpoint" {
  value     = aws_db_instance.warehouse.endpoint
  sensitive = true
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
