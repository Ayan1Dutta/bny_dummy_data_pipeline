from __future__ import annotations

import argparse
import json
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Render an ECS task definition for the settlement API")
    parser.add_argument("--image", required=True)
    parser.add_argument("--env", required=True, choices=["dev", "test", "prod"])
    parser.add_argument("--cpu", default="512")
    parser.add_argument("--memory", default="1024")
    args = parser.parse_args()

    account = os.environ["AWS_ACCOUNT_ID"]
    region = os.environ.get("AWS_REGION", "ap-south-1")
    prefix = f"arn:aws:secretsmanager:{region}:{account}:secret:settlement/{args.env}"

    task_def = {
        "family": f"settlement-api-{args.env}",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": args.cpu,
        "memory": args.memory,
        "executionRoleArn": f"arn:aws:iam::{account}:role/settlement-{args.env}-execution",
        "taskRoleArn": f"arn:aws:iam::{account}:role/settlement-{args.env}-task",
        "containerDefinitions": [
            {
                "name": "api",
                "image": args.image,
                "essential": True,
                "portMappings": [{"containerPort": 8000, "protocol": "tcp"}],
                "environment": [
                    {"name": "APP_ENV", "value": args.env},
                    {"name": "LOG_LEVEL", "value": "INFO" if args.env == "prod" else "DEBUG"},
                    {"name": "CORS_ORIGINS", "value": f"https://settlement-{args.env}.internal.bny"},
                    {"name": "WAREHOUSE_DSN_SSM", "value": f"/settlement/{args.env}/warehouse"},
                ],
                "secrets": [
                    {"name": "API_KEY", "valueFrom": f"{prefix}/api-key"},
                    {"name": "PII_SALT", "valueFrom": f"{prefix}/pii-salt"},
                    {"name": "WAREHOUSE_PASSWORD", "valueFrom": f"{prefix}/warehouse-password"},
                ],
                "healthCheck": {
                    "command": ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')\""],
                    "interval": 30,
                    "timeout": 5,
                    "retries": 3,
                    "startPeriod": 20,
                },
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": f"/ecs/settlement-api-{args.env}",
                        "awslogs-region": region,
                        "awslogs-stream-prefix": "api",
                    },
                },
            }
        ],
    }
    print(json.dumps(task_def))


if __name__ == "__main__":
    main()
