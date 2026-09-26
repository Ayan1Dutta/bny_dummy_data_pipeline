# 14 — Production Readiness

## Requirement status

| Requirement | Target | Result | Evidence |
|---|---|---|---|
| Test suite | 100% passing | 106/106 (100%) | `evidence/01_test_results/summary.json` |
| Critical security findings | 0 | 0 blocking | `evidence/02_security_results/summary.json` |
| API health | HTTP 200 | 200 | smoke reports |
| API response time | < 500 ms | p95 well under budget (tests assert p95 < 500 ms) | `tests/test_performance.py` |
| Deployment | GitLab CI | 8-stage pipeline | `.gitlab-ci.yml` |
| Secondary build | Jenkins | Checkout → Publish | `Jenkinsfile` |
| Strategy | Blue-Green | 2 app containers + nginx container locally; AWS ALB listener switch in prod | `docker-compose.bluegreen.yml`, `deploy/`, `cutover_prod` job |
| Rollback | Required | Verified 99.21% → 92.40% | `evidence/10_incident/` |
| Secrets | None hard-coded | App refuses to start in test/prod with placeholders | `src/config.py`, `tests/test_production_config.py` |
| Smoke test | Mandatory | 11 checks incl. KPI values | `scripts/smoke_test.py` |
| Reconciliation | Mandatory | Independent recompute from facts | `scripts/reconciliation_gate.py` |

## Test gate

Run order: Unit → Data Model → Pipeline → API → Business → Security. The gate stops at the first failing stage.

| Stage | Tests | What it proves |
|---|---|---|
| unit | 17 | Validation rules, dedup, late/out-of-order events |
| data_model | 18 | Fact grain, duplicates, referential integrity, reconciliation |
| pipeline | 11 | Idempotency, incremental loads, gates detect inflation and fan-out |
| api | 21 | 200 / 400 / 404 / 422, contract, p95 latency < 500 ms |
| business | 12 | Settlement rate, gap, SLA, exception OR logic, **mandatory multi-settlement negative test** |
| security | 27 | Injection, PII, auth, secret-scan rules, SAST triage, production config |

Stages are assigned by the root `conftest.py` (by file name), so `pytest -m business` runs one stage.

### Mandatory negative test

`test_mandatory_negative_three_settlements_do_not_triple_the_settlement_rate`: one ₹9,000 transaction settled in three ₹3,000 legs. It first proves the trap is real (a join before aggregation gives 280%), then asserts the API returns exactly 100%. A second test asserts the shared fixture still contains a split settlement, so nobody can "fix" the suite by deleting the hard case.

## Security gate

`python -m scripts.security_gate` (or `--checks sast|secrets|sca` for one check).

| Check | Tool | Blocks when | Current result |
|---|---|---|---|
| SAST | bandit | Any HIGH; any MEDIUM not triaged; any SQL-string finding in `src/api/` | 17 MEDIUM B608, all triaged in `security/sast_triage.json`; 0 in API layer |
| Secrets | pattern scan + detect-secrets | Any finding not in `security/secrets_allowlist.json` within its count | 0 blocking, 5 allowlisted (fake test values, Jenkins variable name) |
| SCA | pip-audit + GitHub/OSV severity | CRITICAL, HIGH or UNKNOWN in runtime deps, unless an unexpired waiver exists | 22 deps, 0 vulnerable |
| Container | trivy | CRITICAL with a fix available | Runs in CI (`container_scan`) |

Why B608 is triaged, not ignored: those strings build SQL from constants in source control (table names, the rule catalogue), and all values are bound parameters. The triage file records a maximum count per file, so a **new** finding still blocks.

Proving SCA works: `python -m scripts.security_gate --checks sca --requirements security/sca_demo/requirements-vulnerable.txt` blocks `pyyaml 5.3` (CRITICAL, fix ≥ 5.4) and `requests 2.19.1` (fix ≥ 2.20.0). Remediation is to upgrade to the fixed version; if none exists, replace the package or raise a time-boxed waiver in `security/sca_waivers.json`. If severity cannot be looked up, it is treated as blocking (fail closed).

## GitLab CI

| Stage | Jobs | Notes |
|---|---|---|
| validate | lint | ruff + compile |
| test | test_gate | JUnit report, KPI reconciliation of the CI warehouse |
| security | sast, secret_scan, sca | Run in parallel |
| build | build_image | Image tagged with commit SHA, pushed to registry |
| package | container_scan, release_manifest | Scan fails on CRITICAL; manifest records image digest |
| deploy_dev | deploy_dev | ECS dev service |
| smoke_test | smoke_test_dev | Business-KPI smoke, not just /health |
| deploy_prod | deploy_prod_green, cutover_prod, rollback_prod | All manual, `resource_group: production` |

Features used: `stages`, `needs` (DAG), `artifacts` (evidence, JUnit, dotenv), `cache` (pip keyed on requirements file), `rules` (MR vs main vs tag), `environment` (dev, production), `when: manual`, `resource_group` (no two prod deploys at once).

Protected variables (Settings → CI/CD → Variables, **Protected + Masked**, only exposed on protected branches and tags): `PROD_SMOKE_API_KEY`, `PROD_LISTENER_ARN`, `PROD_BLUE_TG_ARN`, `PROD_GREEN_TG_ARN`, `PROD_BLUE_TEST_URL`, `PROD_GREEN_TEST_URL`, `PROD_API_URL`, AWS role credentials. Application secrets (API key, PII salt, DB password) live in AWS Secrets Manager and are injected into the ECS task, never into the repo or the image.

Approval: `production` is a protected environment with required approvers, on top of `when: manual`.

## Jenkins as the secondary path

`Jenkinsfile`: Checkout → Install → Run Tests → Security (SAST / Secret / SCA in parallel) → Build Image → Container Scan → Publish Artifact. It runs the same scripts as GitLab, so both paths apply the same gates. By default it archives the image as a build artifact; it only pushes when `PUBLISH=true`.

Why keep Jenkins alongside GitLab CI:
- **Continuity** — if GitLab runners or SaaS are down, a verified build can still be produced.
- **Legacy estate** — many enterprise jobs, agents and credential stores already live in Jenkins; migrating everything at once is risky.
- **Special agents** — some regulated or on-prem build agents are only wired into Jenkins.
- **Independent verification** — two systems building the same commit SHA and getting the same test and scan results is a reproducibility check.

Guardrail: only GitLab CI deploys to production. Jenkins produces an artifact; it never touches the load balancer.
