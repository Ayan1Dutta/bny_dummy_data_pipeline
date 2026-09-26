from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from src.config import utcnow

PROJECT = Path(__file__).resolve().parents[1]
EVIDENCE = PROJECT / "evidence"
STATE = PROJECT / "deploy" / "state.json"
HOST = os.getenv("BLUEGREEN_HOST", "127.0.0.1")


def latest_runs(label: str) -> list[Path]:
    runs = EVIDENCE / "runs"
    return sorted(runs.glob(f"*_{label}")) if runs.exists() else []


def write(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content, indent=2, default=str)
    path.write_text(text, encoding="utf-8")


def live_json(url: str) -> dict:
    try:
        r = httpx.get(url, timeout=3)
        return {"http_status": r.status_code, "body": r.json()}
    except (httpx.HTTPError, ValueError) as exc:
        return {"error": str(exc)}


def ensure(stage_dir: str, module: str) -> str:
    target = EVIDENCE / stage_dir
    if (target / "summary.json").exists():
        return "collected"
    code = subprocess.run([sys.executable, "-m", module, "--out", str(target)], cwd=PROJECT).returncode
    return "generated" if code == 0 else "generated (FAILED)"


def gitlab_pipeline() -> str:
    out = EVIDENCE / "03_gitlab_pipeline"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT / ".gitlab-ci.yml", out / "gitlab-ci.yml")
    text = (PROJECT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    stages = re.search(r"^stages:\n((?:\s+- .+\n)+)", text, flags=re.M)
    jobs = re.findall(r"^([a-z_]+):\n\s+stage: (\S+)", text, flags=re.M)
    extended = re.findall(r"^([a-z_]+):\n\s+extends: \.security_job", text, flags=re.M)
    lines = ["# GitLab CI pipeline", "", "Stages: " + ", ".join(s.strip("- ").strip() for s in stages.group(1).splitlines()), "",
             "| Job | Stage | Manual |", "|---|---|---|"]
    for job, stage in jobs:
        block = text.split(f"\n{job}:\n", 1)[1].split("\n\n", 1)[0]
        lines.append(f"| {job} | {stage} | {'yes' if 'when: manual' in block else 'no'} |")
    lines += [f"| {job} | security | no |" for job in extended]
    lines += ["", "Attach the pipeline screenshot and URL from GitLab (CI/CD > Pipelines) as pipeline_run.png / pipeline_url.txt."]
    write(out / "pipeline_summary.md", "\n".join(lines))
    return "collected"


def jenkins_pipeline() -> str:
    out = EVIDENCE / "04_jenkins_pipeline"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT / "Jenkinsfile", out / "Jenkinsfile")
    stages = re.findall(r"stage\('([^']+)'\)", (PROJECT / "Jenkinsfile").read_text(encoding="utf-8"))
    write(out / "pipeline_summary.md", "# Jenkins secondary build\n\nStages: " + " -> ".join(stages)
          + "\n\nAttach the Jenkins stage view screenshot and console log as stage_view.png / console.log.")
    return "collected"


def docker_image(state: dict) -> str:
    out = EVIDENCE / "05_docker_image"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT / "Dockerfile", out / "Dockerfile")
    shutil.copy2(PROJECT / "docker-compose.bluegreen.yml", out / "docker-compose.bluegreen.yml")
    write(out / "release_images.json", state.get("releases", {}))
    if not shutil.which("docker"):
        return "collected (release records only; docker not available)"
    inspected = {}
    for name, info in state.get("releases", {}).items():
        proc = subprocess.run(["docker", "image", "inspect", info["image"]], capture_output=True, text=True)
        if proc.returncode == 0:
            data = json.loads(proc.stdout)[0]
            inspected[name] = {"image": info["image"], "current_id": data["Id"], "approved_id": info.get("image_id"),
                               "matches_approved": data["Id"] == info.get("image_id"), "created": data.get("Created"),
                               "labels": (data.get("Config") or {}).get("Labels"), "size_bytes": data.get("Size")}
    write(out / "image_inspect.json", inspected)
    return f"collected ({len(inspected)} images inspected)"


def deployment(state: dict) -> str:
    out = EVIDENCE / "06_deployment_version"
    colors = {}
    for color, info in state.get("colors", {}).items():
        keys = ("release", "version", "image", "image_id", "approved_config_fingerprint", "tampered", "port", "started_at", "warehouse")
        colors[color] = {**{k: info.get(k) for k in keys},
                         "approved_image_id": state.get("releases", {}).get(info.get("release"), {}).get("image_id"),
                         "live_health": live_json(f"http://{HOST}:{info['port']}/health")}
    write(out / "deployment_versions.json", {"captured_at": utcnow().isoformat(), "active": state.get("active"), "colors": colors})
    return "collected"


def blue_green(state: dict) -> str:
    out = EVIDENCE / "07_blue_green_status"
    safe_state = {k: v for k, v in state.items() if k != "history"}
    write(out / "state.json", safe_state)
    write(out / "load_balancer_status.json", live_json(f"http://{HOST}:8000/lb/status"))
    if shutil.which("docker"):
        compose = ["docker", "compose", "-f", str(PROJECT / "docker-compose.bluegreen.yml")]
        conf = subprocess.run([*compose, "exec", "-T", "lb", "cat", "/etc/nginx/bluegreen/active.conf"], capture_output=True, text=True)
        if conf.returncode == 0:
            write(out / "nginx_active.conf", conf.stdout)
        ps = subprocess.run([*compose, "ps", "--format", "json"], capture_output=True, text=True)
        if ps.returncode == 0:
            write(out / "containers.json", ps.stdout)
    for run in latest_runs("precheck_green") + latest_runs("precheck_blue"):
        shutil.copy2(run / "precheck.json", out / f"{run.name}.json")
    return "collected"


def copy_runs(target: str, filename: str) -> str:
    out = EVIDENCE / target
    out.mkdir(parents=True, exist_ok=True)
    copied = 0
    for label in ("precheck_green", "precheck_blue", "post_cutover", "rollback"):
        for run in latest_runs(label):
            if (run / filename).exists():
                shutil.copy2(run / filename, out / f"{run.name}.json")
                copied += 1
    return f"collected ({copied} runs)"


def incident(state: dict) -> str:
    out = EVIDENCE / "10_incident"
    history = state.get("history", [])
    start = next((i for i, e in enumerate(history) if e["event"] in ("cutover", "cutover_forced")), 0)
    lines = ["# Incident timeline (from deployment controller log)", "", "| Time | Event | Detail |", "|---|---|---|"]
    lines += [f"| {e['ts'][11:]} | {e['event']} | {e['detail']} |" for e in history[start:]]
    write(out / "timeline.md", "\n".join(lines))
    write(out / "full_history.json", history)
    for run in latest_runs("rollback"):
        shutil.copy2(run / "rollback.json", out / f"{run.name}.json")
    detection = EVIDENCE / "runs" / "incident_detection.json"
    if detection.exists():
        shutil.copy2(detection, out / "detection_reconciliation.json")
    return "collected"


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble the production evidence package")
    parser.parse_args()
    EVIDENCE.mkdir(exist_ok=True)
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"colors": {}, "history": []}

    status = {
        "01_test_results": ensure("01_test_results", "scripts.run_test_gate"),
        "02_security_results": ensure("02_security_results", "scripts.security_gate"),
        "03_gitlab_pipeline": gitlab_pipeline(),
        "04_jenkins_pipeline": jenkins_pipeline(),
        "05_docker_image": docker_image(state),
        "06_deployment_version": deployment(state),
        "07_blue_green_status": blue_green(state),
        "08_smoke_test_results": copy_runs("08_smoke_test_results", "smoke_test.json"),
        "09_kpi_reconciliation": copy_runs("09_kpi_reconciliation", "kpi_reconciliation.json"),
        "10_incident": incident(state),
    }
    baseline = EVIDENCE / "baseline_kpis.json"
    if baseline.exists():
        shutil.copy2(baseline, EVIDENCE / "09_kpi_reconciliation" / "baseline_kpis.json")

    tests = json.loads((EVIDENCE / "01_test_results" / "summary.json").read_text(encoding="utf-8"))
    security = json.loads((EVIDENCE / "02_security_results" / "summary.json").read_text(encoding="utf-8"))
    index = ["# Production evidence package", "", f"Generated {utcnow().isoformat()} UTC", "",
             f"- Tests: {tests['total_tests']} run, {tests['failed_tests']} failed, pass rate {tests['pass_rate_pct']}%",
             f"- Security: {security['critical_findings']} blocking findings ({security['results']})",
             f"- Live colour: {state.get('active')}", "", "| Folder | Status |", "|---|---|"]
    index += [f"| {k} | {v} |" for k, v in status.items()]
    write(EVIDENCE / "INDEX.md", "\n".join(index))
    print("\n".join(index))
    return 0


if __name__ == "__main__":
    sys.exit(main())
