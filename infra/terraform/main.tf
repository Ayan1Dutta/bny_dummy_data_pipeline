terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
  backend "s3" {
    bucket         = "bny-terraform-state"
    key            = "settlement-intelligence/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "bny-terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Application = "merchant-settlement-intelligence"
      Owner       = "payments-data-engineering"
      Environment = var.environment
      DataClass   = "confidential"
    }
  }
}

locals {
  name = "settlement-${var.environment}"
}

# ------------------------------------------------------------------ storage
resource "aws_kms_key" "data" {
  description             = "${local.name} data encryption key"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_s3_bucket" "landing" {
  bucket = "${local.name}-landing"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "landing" {
  bucket = aws_s3_bucket.landing.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.data.arn
      sse_algorithm     = "aws:kms"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "landing" {
  bucket                  = aws_s3_bucket.landing.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "landing" {
  bucket = aws_s3_bucket.landing.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "landing" {
  bucket = aws_s3_bucket.landing.id
  rule {
    id     = "archive-raw"
    status = "Enabled"
    filter {
      prefix = "bronze/"
    }
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    expiration {
      days = 2555
    }
  }
}

resource "aws_s3_bucket" "lakehouse" {
  bucket = "${local.name}-lakehouse"
}

resource "aws_s3_bucket_server_side_encryption_configuration" "lakehouse" {
  bucket = aws_s3_bucket.lakehouse.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.data.arn
      sse_algorithm     = "aws:kms"
    }
  }
}

# ------------------------------------------------------------------ warehouse
resource "aws_db_subnet_group" "warehouse" {
  name       = "${local.name}-warehouse"
  subnet_ids = var.private_subnet_ids
}

resource "aws_security_group" "warehouse" {
  name   = "${local.name}-warehouse"
  vpc_id = var.vpc_id

  ingress {
    description     = "PostgreSQL from application tier only"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.api.id, aws_security_group.etl.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "warehouse" {
  identifier                      = "${local.name}-warehouse"
  engine                          = "postgres"
  engine_version                  = "15.7"
  instance_class                  = var.db_instance_class
  allocated_storage               = var.db_allocated_storage
  storage_encrypted               = true
  kms_key_id                      = aws_kms_key.data.arn
  db_subnet_group_name            = aws_db_subnet_group.warehouse.name
  vpc_security_group_ids          = [aws_security_group.warehouse.id]
  multi_az                        = var.environment == "prod"
  backup_retention_period         = var.environment == "prod" ? 35 : 7
  deletion_protection             = var.environment == "prod"
  performance_insights_enabled    = true
  enabled_cloudwatch_logs_exports = ["postgresql"]
  manage_master_user_password     = true
  username                        = "settlement_admin"
  db_name                         = "settlement"
  skip_final_snapshot             = var.environment != "prod"
}

# ------------------------------------------------------------------ secrets
resource "aws_secretsmanager_secret" "api_key" {
  name       = "settlement/${var.environment}/api-key"
  kms_key_id = aws_kms_key.data.arn
}

resource "aws_secretsmanager_secret" "pii_salt" {
  name       = "settlement/${var.environment}/pii-salt"
  kms_key_id = aws_kms_key.data.arn
}

# ------------------------------------------------------------------ container platform
resource "aws_ecr_repository" "api" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.data.arn
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_security_group" "api" {
  name   = "${local.name}-api"
  vpc_id = var.vpc_id

  ingress {
    description     = "HTTP from the internal load balancer"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "etl" {
  name   = "${local.name}-etl"
  vpc_id = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "alb" {
  name   = "${local.name}-alb"
  vpc_id = var.vpc_id

  ingress {
    description = "HTTPS from the corporate network"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.corporate_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "api" {
  name               = "${local.name}-alb"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.private_subnet_ids
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name}-tg"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_ecs_service" "api" {
  name            = "settlement-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = var.api_task_definition_arn
  desired_count   = var.environment == "prod" ? 3 : 1
  launch_type     = "FARGATE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
}

# ------------------------------------------------------------------ pipeline orchestration
resource "aws_cloudwatch_event_rule" "nightly_pipeline" {
  name                = "${local.name}-nightly"
  description         = "Run the settlement pipeline every 15 minutes during the settlement window"
  schedule_expression = var.pipeline_schedule
}

resource "aws_cloudwatch_event_target" "nightly_pipeline" {
  rule     = aws_cloudwatch_event_rule.nightly_pipeline.name
  arn      = aws_ecs_cluster.main.arn
  role_arn = var.events_role_arn

  ecs_target {
    task_definition_arn = var.etl_task_definition_arn
    launch_type         = "FARGATE"
    network_configuration {
      subnets          = var.private_subnet_ids
      security_groups  = [aws_security_group.etl.id]
      assign_public_ip = false
    }
  }
}

resource "aws_lambda_function" "file_trigger" {
  function_name = "${local.name}-file-trigger"
  role          = var.lambda_role_arn
  runtime       = "python3.12"
  handler       = "lambda_s3_trigger.handler"
  filename      = var.lambda_package_path
  timeout       = 60

  environment {
    variables = {
      ETL_CLUSTER         = aws_ecs_cluster.main.name
      ETL_TASK_DEFINITION = var.etl_task_definition_arn
      SUBNET_IDS          = join(",", var.private_subnet_ids)
      SECURITY_GROUP_ID   = aws_security_group.etl.id
    }
  }
}

resource "aws_s3_bucket_notification" "landing" {
  bucket = aws_s3_bucket.landing.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.file_trigger.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "incoming/"
    filter_suffix       = ".csv"
  }
}

# ------------------------------------------------------------------ observability
resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/settlement-api-${var.environment}"
  retention_in_days = var.environment == "prod" ? 365 : 30
  kms_key_id        = aws_kms_key.data.arn
}

resource "aws_sns_topic" "alerts" {
  name              = "${local.name}-alerts"
  kms_master_key_id = aws_kms_key.data.id
}

resource "aws_cloudwatch_metric_alarm" "pipeline_failure" {
  alarm_name          = "${local.name}-pipeline-failed"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "PipelineFailures"
  namespace           = "Settlement/ETL"
  period              = 900
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "The settlement pipeline reported a failed run"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "settlement_rate_drop" {
  alarm_name          = "${local.name}-settlement-rate-drop"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "SettlementRate"
  namespace           = "Settlement/KPI"
  period              = 3600
  statistic           = "Average"
  threshold           = 95
  alarm_description   = "Platform wide settlement rate fell below 95 percent for two hours"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "breaching"
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
  }
  alarm_actions = [aws_sns_topic.alerts.arn]
}
