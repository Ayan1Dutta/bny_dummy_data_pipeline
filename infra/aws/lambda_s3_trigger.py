from __future__ import annotations

import json
import os
import re
import urllib.parse

import boto3

ecs = boto3.client("ecs")

VALID_PREFIX = re.compile(r"^incoming/(transactions|settlements|merchant|payment_events)[\w\-.]*\.csv$")


def handler(event, context):
    started = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        if not VALID_PREFIX.match(key):
            print(json.dumps({"skipped": key, "reason": "unrecognised object key"}))
            continue

        response = ecs.run_task(
            cluster=os.environ["ETL_CLUSTER"],
            taskDefinition=os.environ["ETL_TASK_DEFINITION"],
            launchType="FARGATE",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": os.environ["SUBNET_IDS"].split(","),
                    "securityGroups": [os.environ["SECURITY_GROUP_ID"]],
                    "assignPublicIp": "DISABLED",
                }
            },
            overrides={
                "containerOverrides": [
                    {
                        "name": "etl",
                        "command": ["python", "-m", "src.pipeline.run_pipeline"],
                        "environment": [
                            {"name": "RAW_S3_BUCKET", "value": bucket},
                            {"name": "RAW_S3_KEY", "value": key},
                        ],
                    }
                ]
            },
            tags=[{"key": "trigger", "value": "s3-object-created"}],
        )
        task_arn = response["tasks"][0]["taskArn"] if response.get("tasks") else None
        started.append({"key": key, "task": task_arn})
        print(json.dumps({"started": key, "task": task_arn}))

    return {"statusCode": 200, "body": json.dumps({"tasks_started": started})}
