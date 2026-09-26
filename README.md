# Bank of New York — Real-Time Merchant Settlement Intelligence Platform

A complete sprint deliverable: specification → data model → pipeline → API → dashboard → tests → deployment design.

**The problem.** ₹48 Cr of transaction volume produced ₹45.8 Cr of settlement, and operations could not say whether the difference was failed payments, pending settlements, processing delays, risk holds or data quality. This platform separates the causes and never silently drops a record.

**On the shipped sample week (01–07 Sep 2026):** ₹43.26 Cr successful volume, ₹39.97 Cr settled, 92.4% settlement rate, ₹3.29 Cr gap, 91.22% SLA — decomposed as 49.9% never settled, 28.1% pending, 20.5% failed settlements, 1.5% partial, with 13 merchants carrying most of it.

---

## Quick start

```bash
pip install -r requirements-dev.txt
python scripts/generate_sample_data.py
python -m src.pipeline.run_pipeline
pytest                                    # 64 tests

make api    # terminal 1 -> http://127.0.0.1:8000/docs
make ui     # terminal 2 -> http://127.0.0.1:8080   (enter API key dev-local-key-change-me in the dashboard)
```

Or `docker compose up --build`.

## What is in here

| Path | Contents |
|---|---|
| `docs/` | One comprehensive markdown answer per sprint question — **start with `docs/00_INTERVIEW_MASTER_GUIDE.md`** |
| `features/` | 25 Gherkin acceptance scenarios, each mapped to a test |
| `sql/` | Reference DDL (01–04), production PostgreSQL DDL with partitioning and grants (90), reconciliation and data quality query pack (91) |
| `src/` | Ingestion, Silver/Gold transforms, data quality rule catalogue, pipeline orchestrator, FastAPI service |
| `frontend/` | Single-file dashboard: KPI cards, two SVG charts, exception table, data quality panel |
| `tests/` | 106 tests in six gate stages: unit, data model, pipeline, API, business rules, security |
| `scripts/` | Data generator, test gate, security gate, smoke test, KPI reconciliation gate, drill runner, evidence collector, ECS task renderer |
| `infra/` | Terraform for AWS, Lambda S3 trigger, Glue PySpark job, Step Functions definition |
| `.gitlab-ci.yml` | validate, test, security, build, package, deploy_dev, smoke_test, deploy_prod (manual), plus blue-green rehearsal and rollback |
| `Jenkinsfile` | Secondary build path: tests, security, image build, container scan, artifact |
| `deploy/`, `incident/` | Blue-green controller, nginx load balancer image and config; incident defect injector |
| `docker-compose.bluegreen.yml` | Two app containers (BLUE v1, GREEN v2) behind one nginx container |
| `security/` | SAST triage, secret allowlist, SCA waiver register, SCA demo file |
| `ci/jenkins/` | Ready-made Jenkins lab (Docker) with python, docker CLI, trivy and plugins |

## Architecture

```
CSV feeds -> BRONZE (raw + lineage) -> SILVER (typed, deduped, validated)
          -> GOLD (star schema + settlement reconciliation + daily aggregate)
          -> API (FastAPI, OpenAPI, API key, rate limit) -> DASHBOARD
```

Control plane throughout: watermarks, file audit, quarantine, rule results, run log.

## The three decisions that matter

1. **Settlements are collapsed to transaction grain once, in the pipeline.** A direct transaction-to-settlement join reports ₹57.49 Cr against a true ₹55.91 Cr — ₹1.59 Cr of money that does not exist. `gold.fact_settlement_reconciliation` is the guard.
2. **Business time drives reporting; processing time drives only pipeline control.** Late and out-of-order data corrects the day it belongs to. A settlement arriving two days late recomputes exactly one date × merchant partition.
3. **Nothing disappears.** Every record is loaded, rejected, quarantined or explicitly deduplicated, with its rule, reason and original payload retained — and `source = loaded + quarantined + deduplicated` is asserted on every commit.

## Production readiness sprint

| Goal | Command |
|---|---|
| Test gate (106 tests, 6 stages) | `python -m scripts.run_test_gate` |
| Security gate (SAST, secrets, SCA) | `python -m scripts.security_gate` |
| Blue-green deployment drill (Docker Desktop required) | `python -m scripts.run_drill` |
| Incident drill with rollback | `python -m scripts.run_drill --inject A` |
| Evidence package | `python -m scripts.collect_evidence` |

Guides: `docs/14_PRODUCTION_READINESS.md`, `docs/15_BLUE_GREEN_AND_INCIDENT_DRILL.md`, `docs/16_INCIDENT_REPORT.md`, `docs/17_EXECUTIVE_PRESENTATION.md`, `docs/18_PEER_REVIEW_360.md`, `docs/19_RUN_ON_GITLAB_AND_JENKINS.md`.
