# 15 — Blue-Green Deployment & Incident Drill (Docker + nginx, Windows)

## What runs

```
Browser / dashboard
        │
        ▼
  nginx container  (lb)          http://localhost:8000
        │  reads /etc/nginx/bluegreen/active.conf  →  "send everything to api-blue"
   ┌────┴─────┐
   ▼          ▼
api-blue    api-green            direct test ports 8001 / 8002
image v1    image v2
   └────┬─────┘
        ▼
 shared Docker volume "warehouse"  (settlement.duckdb, read-only access)
```

- **Two app containers** built from the same Dockerfile: `settlement-api:v1` (BLUE) and `settlement-api:v2` (GREEN).
- **One nginx container**. Its config says which colour gets traffic.
- **Switching** = the controller rewrites one small file inside nginx (`active.conf`), runs `nginx -t` (syntax check) and `nginx -s reload`. Reload is graceful: in-flight requests finish on the old colour, new requests go to the new one. In the drill, 979 requests during a rollback all returned HTTP 200.
- If `nginx -t` fails (for example the target container is not running), the old file is restored and traffic does not move.

Files: `docker-compose.bluegreen.yml`, `deploy/nginx/` (Dockerfile, `default.conf`, `active.conf`), `deploy/bluegreen.py` (controller), `deploy/env/blue.env` and `green.env` (per-colour config, created on first start).

## What "fingerprint" means

A fingerprint is a SHA-256 hash: change one character and the hash changes completely. The controller uses two:

| Fingerprint | What it covers | Recorded when | Checked when |
|---|---|---|---|
| **Image ID** (`sha256:…` Docker gives every image) | All code and dependencies inside the image | `release` builds the image after the test gate passes | every `start`, `precheck`, `cutover` |
| **Config fingerprint** (SHA-256 of `deploy/env/<colour>.env`) | Runtime settings such as which database to use | first `start` of a release | every `start`, `precheck`, `cutover` |

If someone rebuilds `settlement-api:v2` or edits `green.env` after approval, the running fingerprint no longer matches the approved one: `status` shows `IMAGE CHANGED` / `CONFIG CHANGED`, the pre-cutover check fails, and `cutover` is refused. This is how defects A, B, D (image changed) and C (config changed) are caught before they can receive traffic.

## Before you start

- Docker Desktop running (`docker version` shows a Server section).
- Ports 8000, 8001, 8002 free. Stop `make api` / `docker compose up` from the other compose file first.
- venv active and the warehouse built:

```powershell
.venv\Scripts\Activate.ps1
uv pip install -r requirements-dev.txt
python scripts/generate_sample_data.py
python -m src.pipeline.run_pipeline
```

## One command

```powershell
python -m scripts.run_drill --inject A
```

Ends with `DRILL PASSED: 13/13`. Add `--keep-running` to leave the containers up so you can look around.

## Step by step

### 1. Build the releases
```powershell
python -m deploy.bluegreen release v1
python -m deploy.bluegreen release v2
```
Each runs the test gate first and refuses to build if it fails. Then `docker build` tags `settlement-api:v1` / `v2` with `APP_VERSION` 1.0.0 / 2.0.0 and records the image ID.

### 2. BLUE live
```powershell
python -m deploy.bluegreen start blue --release v1
python -m deploy.bluegreen lb start
python -m deploy.bluegreen baseline
python -m deploy.bluegreen status
```
The first start copies `data\warehouse\settlement.duckdb` into the shared volume. Open http://localhost:8000/lb/status → `{"active":"blue"}`.

### 3. GREEN, pre-cutover checks, cutover
```powershell
python -m deploy.bluegreen start green --release v2
python -m deploy.bluegreen precheck green
python -m deploy.bluegreen cutover green
```
`precheck` tests GREEN directly on port 8002, before it has any traffic: artifact integrity, configuration integrity, health, database, pipeline status, test record, smoke test, KPI reconciliation. `cutover` needs a passing precheck for the exact image and config now running, less than 2 hours old.

See the switch yourself:
```powershell
docker compose -f docker-compose.bluegreen.yml exec lb cat /etc/nginx/bluegreen/active.conf
curl.exe -i http://localhost:8000/health
```
The response header `X-Served-By: green` and `"version":"2.0.0"` confirm it.

### 4. Break production (facilitator)
```powershell
python -m incident.inject_defect --release v2 --defect A
python -m deploy.bluegreen restart green
```
A, B, D rebuild `settlement-api:v2` with a code defect (image fingerprint changes). C points `green.env` at a stale `settlement_db_v2.duckdb` (config fingerprint changes).

### 5. Detect and investigate
```powershell
python -m deploy.bluegreen note "KPI anomaly detected"
python -m scripts.reconciliation_gate --api-url http://127.0.0.1:8000 --baseline evidence/baseline_kpis.json --out evidence/runs/incident_detection.json
python -m deploy.bluegreen status
python -m deploy.bluegreen diff v1 v2
python -m deploy.bluegreen logs green --tail 50
```
`diff` extracts `/app/src` from both images and shows exactly what changed.

### 6. Decide and roll back
```powershell
python -m deploy.bluegreen note "Root cause: ..."
python -m deploy.bluegreen note "Decision: rollback because ..."
python -m deploy.bluegreen rollback --reason "INC-... grain defect"
```
nginx is switched back to `api-blue`, then the smoke test and KPI reconciliation run through port 8000 and print the rate before and after.

### 7. Evidence and teardown
```powershell
python -m scripts.collect_evidence
python -m deploy.bluegreen timeline
python -m deploy.bluegreen down
```
`down` removes the containers and volumes; the images stay.

## Command reference

| Command | Does |
|---|---|
| `release <name>` | test gate → `docker build` → record image ID |
| `start <colour> --release <name>` | create/replace that colour's container |
| `restart <colour>` | recreate it (picks up a retagged image or edited config) |
| `stop <colour> [--force]` | stop a colour (refuses the live one without `--force`) |
| `lb start` / `lb stop` | start/stop nginx |
| `baseline` | save known-good KPIs from the live colour |
| `precheck <colour>` | all pre-cutover checks on the idle colour |
| `cutover <colour>` | switch nginx to that colour |
| `rollback --reason "..."` | switch back and verify |
| `status`, `timeline`, `logs <colour|lb>`, `diff <a> <b>`, `note "..."` | investigation |
| `seed` | copy the warehouse into the volume again after re-running the pipeline |
| `down` | remove containers and volumes |

## What each defect looks like

| Defect | /health | What the API shows | Caught by |
|---|---|---|---|
| A grain join | 200 | Rate 92.40% → **99.21%**; 19 merchants HTTP 500; 11 wrong | Reconciliation (4 checks), baseline drift, image fingerprint |
| B ingestion date | 200 | 0 transactions, rate 0% | Smoke KPI match, reconciliation, image fingerprint |
| C wrong database | 200 | Rate **53.01%** from stale `settlement_db_v2` | Reconciliation, config fingerprint, `status` shows the database |
| D rate / count | 200 | Summary HTTP 500 | Smoke test, reconciliation, image fingerprint |

## Local drill vs AWS production

| Local | Production (GitLab `cutover_prod`) |
|---|---|
| nginx container | AWS Application Load Balancer |
| `api-blue` / `api-green` containers | two ECS services / target groups |
| `active.conf` + `nginx -s reload` | `aws elbv2 modify-listener` |
| image ID check | ECR image digest pinned in the task definition |
| `deploy/state.json` active colour | SSM parameter `/settlement/prod/live_color` |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker not found` / cannot connect | Start Docker Desktop and wait until it says "running" |
| `port is already allocated` | Something uses 8000–8002: stop it, or `python -m deploy.bluegreen down` |
| `pull access denied for settlement-api` | Build first: `python -m deploy.bluegreen release v1` |
| `refusing cutover` | Run `precheck` again; the image or config changed, or the check is older than 2 hours |
| 502 from nginx | Live colour is down: `status`, then `restart <colour>` |
| Company network blocks Docker Hub | Point at your internal mirror: `$env:PYTHON_IMAGE="mirror/python:3.12-slim"; $env:NGINX_IMAGE="mirror/nginx:1.27-alpine"` |
