variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "environment" {
  type        = string
  description = "dev | test | prod"
  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "environment must be dev, test or prod."
  }
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "corporate_cidrs" {
  type        = list(string)
  description = "CIDR ranges allowed to reach the internal load balancer"
}

variable "certificate_arn" {
  type = string
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_allocated_storage" {
  type    = number
  default = 100
}

variable "api_task_definition_arn" {
  type = string
}

variable "etl_task_definition_arn" {
  type = string
}

variable "events_role_arn" {
  type = string
}

variable "lambda_role_arn" {
  type = string
}

variable "lambda_package_path" {
  type    = string
  default = "build/file_trigger.zip"
}

variable "pipeline_schedule" {
  type        = string
  default     = "rate(15 minutes)"
  description = "EventBridge schedule for the incremental pipeline"
}
