# 19 — Run the Pipeline on GitLab CI and Jenkins

## Before you start (once, on your machine)

```powershell
uv venv
.venv\Scripts\Activate.ps1
uv pip install -r requirements-dev.txt
python scripts/generate_sample_data.py
python -m src.pipeline.run_pipeline
python -m scripts.run_test_gate
python -m scripts.security_gate
python -m scripts.run_drill --inject A
```

If all three end in `PASS` / `DRILL PASSED`, the pipelines will pass too, because they run the same scripts.

Put the code in git:

```powershell
git init -b main
git add .
git commit -m "Settlement platform with production readiness"
```

---

## Part A — GitLab CI (primary path)

### A1. Create the project and push

1. On gitlab.com: **New project → Create blank project** → name `bny-settlement-platform` → untick "Initialize with README".
2. Push:

```powershell
git remote add origin https://gitlab.com/<your-username>/bny-settlement-platform.git
git push -u origin main
```

The pipeline starts automatically. Open **Build → Pipelines**.

### A2. Runners

gitlab.com shared runners work out of the box (they support Docker-in-Docker for `build_image`). If your project says "no runners", enable them in **Settings → CI/CD → Runners → Enable instance runners**. New gitlab.com accounts may need to verify identity with a card before shared runners run.

### A3. What runs without AWS

| Stage | Job | Needs AWS? |
|---|---|---|
| validate | lint | No |
| test | test_gate | No |
| security | sast, secret_scan, sca | No |
| build | build_image | No (pushes to the GitLab registry) |
| package | container_scan, release_manifest | No |
| smoke_test | **bluegreen_rehearsal** — builds v1/v2 images, runs blue + green + nginx containers in Docker-in-Docker, injects an incident, detects it, rolls back | No |
| deploy_dev, smoke_test_dev | ECS dev deploy | Yes |
| deploy_prod | deploy_prod_green, cutover_prod, rollback_prod (manual) | Yes |

AWS jobs only appear when the `AWS_ACCOUNT_ID` variable exists, so without AWS the pipeline finishes green after `bluegreen_rehearsal`.

### A4. Download the evidence

Open a job → right panel **Job artifacts → Browse / Download**:

| Job | Evidence |
|---|---|
| test_gate | `evidence/01_test_results/` + **Tests** tab on the pipeline |
| sast / secret_scan / sca | `evidence/02_security_results/` |
| container_scan | `evidence/02_security_results/container.json` |
| build_image | `evidence/05_docker_image/image_digest.txt` |
| bluegreen_rehearsal | `evidence/06`…`10`, `INDEX.md`, incident timeline |

Take a screenshot of the pipeline graph for `evidence/03_gitlab_pipeline/`.

### A5. Turning on AWS deployment (optional)

**Settings → CI/CD → Variables**, tick **Protected** and **Masked** where possible:

| Variable | Value |
|---|---|
| `AWS_ACCOUNT_ID` | 12-digit account id |
| `AWS_REGION` | e.g. `ap-south-1` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | deploy user or OIDC role credentials |
| `DEV_API_URL`, `DEV_SMOKE_API_KEY` | dev ALB URL and key |
| `PROD_API_URL`, `PROD_SMOKE_API_KEY` | prod ALB URL and key |
| `PROD_LISTENER_ARN`, `PROD_BLUE_TG_ARN`, `PROD_GREEN_TG_ARN` | ALB listener and target groups |
| `PROD_BLUE_TEST_URL`, `PROD_GREEN_TEST_URL` | direct URL of each colour |

Also create SSM parameter `/settlement/prod/live_color` = `blue`, and in **Settings → CI/CD → Protected environments** protect `production` with required approvers. Infrastructure is in `infra/terraform/`.

Then on the pipeline: `deploy_prod_green` ▶ → check its smoke report → `cutover_prod` ▶. If anything is wrong: `rollback_prod` ▶.

---

## Part B — Jenkins (secondary path)

Requires Docker Desktop running.

### B1. Start the ready-made Jenkins

```powershell
docker compose -f ci/jenkins/docker-compose.yml up -d --build
docker exec bny-jenkins cat /var/jenkins_home/secrets/initialAdminPassword
```

Open http://localhost:8090, paste the password, choose **Install suggested plugins** (the required ones are already baked in), create your admin user.

The image already contains python3, the docker CLI and trivy, and your project folder is mounted read-only at `/repo`.

### B2. Create the job

1. **New Item** → name `bny-settlement-platform` → **Pipeline** → OK.
2. **Pipeline** section → Definition: **Pipeline script from SCM** → SCM: **Git**.
3. Repository URL — pick one:
   - Local folder (no GitLab needed): `file:///repo`
   - GitLab: `https://gitlab.com/<your-username>/bny-settlement-platform.git` (add credentials if private)
4. Branch: `*/main` · Script Path: `Jenkinsfile` → **Save**.

Jenkins builds the last **commit**, so commit any change before building.

### B3. Run it

**Build with Parameters** → leave `PUBLISH` unticked → **Build**.

Stages: Checkout → Install Dependencies → Run Tests → Security Checks (SAST / Secret Scan / SCA in parallel) → Build Docker Image → Container Scan → Publish Artifact.

With `PUBLISH` unticked the image is saved as `settlement-api-<sha>.tar.gz` in the build artifacts.

### B4. Evidence

- Build page → **Test Result** (106 tests)
- **Artifacts** → `evidence/**` and the image tarball
- Screenshot the **Stage View** for `evidence/04_jenkins_pipeline/`

### B5. Publishing to the registry (optional)

Create a GitLab deploy token (**Settings → Repository → Deploy tokens**, scope `write_registry`). In Jenkins: **Manage Jenkins → Credentials → Global → Add** → *Username with password*, ID `gitlab-registry-deploy-token`. Update `IMAGE_REPO` in the `Jenkinsfile` to your registry path, then build with `PUBLISH` ticked.

### B6. Stop Jenkins

```powershell
docker compose -f ci/jenkins/docker-compose.yml down
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| GitLab job stuck "pending" | No runner: enable instance runners or verify your account |
| `build_image` or `bluegreen_rehearsal` cannot connect to docker | Runner must allow privileged Docker-in-Docker (gitlab.com shared runners do) |
| `bluegreen_rehearsal` cannot reach port 8000 | Keep `BLUEGREEN_HOST: docker` and `BIND_ADDR: 0.0.0.0` in the job; containers run on the dind host, not localhost |
| `container_scan` fails on CRITICAL | A fixed version exists: rebuild (the Dockerfile upgrades OS packages) or bump the base image. CVEs with no fix do not block (`--ignore-unfixed`) |
| `sca` shows UNKNOWN severity and fails | Advisory lookup blocked by network; rerun, or allow `api.github.com` / `api.osv.dev` |
| Jenkins `python3: not found` | You are not using `ci/jenkins` image; rebuild with `docker compose ... up -d --build` |
| Jenkins `docker: permission denied` | The compose file runs Jenkins as root with the docker socket mounted; keep `user: root` |
| Jenkins "Couldn't find any revision to build" | Branch name mismatch: use `*/main` and make sure you committed |
| Port 8090 busy | Change `127.0.0.1:8090:8080` in `ci/jenkins/docker-compose.yml` |
