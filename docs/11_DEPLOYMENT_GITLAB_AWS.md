# Task 4 Part E — Deployment: GitLab CI/CD and AWS

> Deliverable for: *"Provide a deployment design. Identify environment configuration, secrets, health check, smoke test, rollback mechanism."*

Code for this section: `.gitlab-ci.yml`, `Dockerfile`, `docker-compose.yml`, `scripts/smoke_test.sh`, `scripts/dq_gate.py`, `scripts/render_task_def.py`, `infra/terraform/*.tf`, `infra/aws/lambda_s3_trigger.py`, `infra/aws/glue_job_bronze_to_silver.py`, `infra/aws/step_function_pipeline.json`.

---

## 1. Pipeline shape

```
  commit / merge request
        |
   VALIDATE   ruff lint
        |
   TEST       pytest (64 tests) + 80% coverage gate
              data-quality gate: run the pipeline, fail on CRITICAL or >2% quarantine
        |
   SECURITY   bandit SAST | pip-audit dependency scan | detect-secrets
        |
   BUILD      docker build, push to GitLab registry and ECR (immutable tag = commit SHA)
        |
   DEV        automatic on default branch -> ECS deploy -> smoke test
        |
   TEST       manual gate -> ECS deploy -> smoke test
        |
   PROD       manual gate -> ECS deploy -> smoke test -> rollback job available
```

Every stage is a gate, not a report: lint, tests, coverage, data quality, SAST, dependency scan and secret detection all fail the build rather than warn.

## 2. Environment configuration

Configuration is environment variables only, read once in `src/config.py`. The same image runs in every environment; nothing is baked in.

| Variable | dev | test | prod |
|---|---|---|---|
| `APP_ENV` | dev | test | prod |
| `LOG_LEVEL` | DEBUG | INFO | INFO |
| `WAREHOUSE_PATH` / DSN | local file | RDS test | RDS prod, Multi-AZ |
| `API_KEYS_ENABLED` | true | true | true (OIDC in front) |
| `CORS_ORIGINS` | localhost:8080 | test dashboard origin | prod dashboard origin |
| `SETTLEMENT_SLA_MINUTES` | 30 | 30 | 30 |
| `RATE_LIMIT_PER_MINUTE` | 1000 | 300 | 120 |
| `MAX_DATE_RANGE_DAYS` | 366 | 366 | 366 |

Business thresholds (SLA minutes, settlement and SLA rate thresholds, lateness thresholds) are configuration rather than code, so Payments Operations can change a policy without a release.

## 3. Secrets

- Stored in AWS Secrets Manager under `settlement/{env}/api-key`, `settlement/{env}/pii-salt`, `settlement/{env}/warehouse-password`, encrypted with a customer-managed KMS key with rotation enabled.
- Injected into the container as ECS task `secrets`, so they exist only in the task's process environment — not in the image, not in the task definition, not in a log.
- GitLab CI holds only deployment credentials, as masked and protected variables scoped to protected branches; `AWS_ACCOUNT_ID`, `SMOKE_API_KEY` and `API_BASE_URL` are per-environment CI variables.
- `detect-secrets` runs on every commit and fails the build on any finding.
- Rotation is a rolling ECS restart; the application reads secrets at start-up.

## 4. Health check and readiness

Two distinct endpoints, because they answer different questions:

| Endpoint | Question | Used by |
|---|---|---|
| `/health` | is the process alive? | ALB target group, container `HEALTHCHECK`, ECS |
| `/ready` | can it actually serve? warehouse reachable, Gold populated, last batch status | deployment gate, monitoring, dashboard staleness badge |

The ALB health check hits `/health` every 30 s, 2 healthy / 3 unhealthy thresholds, 30 s deregistration delay. A task failing health checks is replaced automatically.

## 5. Smoke test

`scripts/smoke_test.sh` runs after every deployment and after every rollback:

1. `/health` returns `status: ok`
2. `/ready` reports the warehouse reachable
3. `/api/v1/settlement-summary` returns 200 and both rates are within 0–100 and the amount is non-negative
4. an unauthenticated request returns 401

Any failure exits non-zero, which fails the deployment job and triggers the rollback path. The check is deliberately business-level, not just "the port is open": a deployment that serves a 104% settlement rate is a failed deployment.

## 6. Rollback

Three layers, fastest first:

1. **ECS deployment circuit breaker** (`enable = true, rollback = true` in Terraform). If new tasks fail health checks, ECS reverts to the previous task definition automatically, with no human involved.
2. **Manual `rollback-prod` job** in GitLab. It reads the previous task definition ARN from the service's deployment history, updates the service back to it, waits for stability and re-runs the smoke test. One click, no rebuild — because images are immutable and tagged by commit SHA, the previous artefact still exists.
3. **Data rollback.** Application rollback does not fix bad data. The recovery path is: fix the transformation, then `--full-refresh` (rebuild reconciliation and aggregate from the facts), or reprocess from Bronze, which is immutable and file-audited. Source systems are never re-asked for files. RDS keeps 35 days of automated backups with point-in-time recovery in production.

Deployment is 100% minimum healthy / 200% maximum percent, so a rolling deploy never reduces capacity.

## 7. AWS architecture

```
  Source systems ---> S3 settlement-{env}-landing/incoming/   (KMS, versioned, Glacier IR after 90d)
                            |
                     S3 event -> Lambda file-trigger --> ECS Fargate ETL task
                            |                                   |
                     EventBridge rate(15 minutes) --------------+
                            |
                            v
                     RDS PostgreSQL (Multi-AZ in prod)  <-- bronze/silver/gold/ctl
                            |
                     ECS Fargate service "settlement-api" (3 tasks in prod)
                            |
                     internal ALB, TLS 1.3, corporate CIDRs only
                            |
                     S3 + CloudFront static dashboard
                            |
                     CloudWatch metrics/alarms -> SNS -> operations
```

| Concern | AWS service | Terraform resource |
|---|---|---|
| Landing zone | S3, KMS, lifecycle to Glacier IR | `aws_s3_bucket.landing` |
| Warehouse | RDS PostgreSQL 15, Multi-AZ in prod, 35 day backups | `aws_db_instance.warehouse` |
| Image registry | ECR, immutable tags, scan on push | `aws_ecr_repository.api` |
| Compute | ECS Fargate cluster, service, circuit breaker | `aws_ecs_cluster.main`, `aws_ecs_service.api` |
| Ingress | internal ALB, TLS 1.3, target group on `/health` | `aws_lb.api`, `aws_lb_listener.https` |
| Scheduling | EventBridge rule every 15 minutes | `aws_cloudwatch_event_rule.nightly_pipeline` |
| Event trigger | S3 → Lambda → `ecs.run_task` | `aws_lambda_function.file_trigger` |
| Orchestration | Step Functions state machine with retry, DQ gate and SNS alerting | `infra/aws/step_function_pipeline.json` |
| Secrets | Secrets Manager + KMS | `aws_secretsmanager_secret.*` |
| Observability | CloudWatch log groups, three alarms, SNS topic | `aws_cloudwatch_metric_alarm.*` |
| Scale-out path | Glue PySpark job implementing the same Silver rules | `infra/aws/glue_job_bronze_to_silver.py` |

Alarms shipped: pipeline failure, platform settlement rate below 95% for two hours, API 5xx above 5 in 5 minutes. The second one is the important one — it alerts on the business metric, not just on infrastructure.

## 8. Why these choices

**ECS Fargate over EKS or EC2** — one API service and one scheduled task do not justify a Kubernetes control plane; Fargate removes patching and gives a native circuit breaker and rollback.

**RDS PostgreSQL over a data warehouse appliance** — the volumes here (millions of rows per day) sit comfortably in partitioned PostgreSQL, the model uses standard SQL, and constraints are enforced by the engine. The Glue + S3 path is pre-written for the day volume outgrows it, and it reuses the same Silver rules rather than a new model.

**Lambda trigger plus scheduled run** — files should be processed when they land, but the platform must also self-heal if an event is missed, so the schedule is a safety net rather than the primary path. Both routes call the same idempotent pipeline, so a double trigger is harmless.

**Step Functions rather than cron inside the container** — retries, catch branches, the data quality gate and SNS alerting become declarative and observable rather than buried in application code.

**Immutable image tags** — a rollback must be able to redeploy an exact artefact. Mutable `latest` tags make "roll back to yesterday" unprovable.

## 9. Deploying it

```bash
# local, no cloud account required
make install && make data && make pipeline && make api     # then make ui

# container
docker compose up --build

# infrastructure
cd infra/terraform
terraform init
terraform plan  -var environment=dev -var vpc_id=... -var 'private_subnet_ids=["subnet-a","subnet-b"]'
terraform apply -var environment=dev ...

# application deploy is the GitLab pipeline; manually it is:
AWS_ACCOUNT_ID=... python scripts/render_task_def.py --image $ECR_URL:$SHA --env dev > taskdef.json
aws ecs register-task-definition --cli-input-json file://taskdef.json
aws ecs update-service --cluster settlement-dev --service settlement-api --force-new-deployment
./scripts/smoke_test.sh https://settlement-dev.internal.bny "$SMOKE_API_KEY"
```
