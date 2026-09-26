from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME_DIRS = ["evidence/runs", "evidence/03_gitlab_pipeline", "evidence/04_jenkins_pipeline", "evidence/05_docker_image",
                "evidence/06_deployment_version", "evidence/07_blue_green_status", "evidence/08_smoke_test_results",
                "evidence/09_kpi_reconciliation", "evidence/10_incident", "deploy/logs", "deploy/env"]
LB_URL = f"http://{os.getenv('BLUEGREEN_HOST', '127.0.0.1')}:8000"
RUNTIME_FILES = ["deploy/state.json", "evidence/baseline_kpis.json", "evidence/INDEX.md", "data/warehouse/settlement_db_v2.duckdb"]


def step(title: str, *args: str, expect_fail: bool = False) -> bool:
    print(f"\n==== {title} ====", flush=True)
    code = subprocess.run([sys.executable, "-m", *args], cwd=PROJECT).returncode
    ok = (code != 0) if expect_fail else (code == 0)
    print(f"---- {title}: {'OK' if ok else 'UNEXPECTED RESULT'} (exit {code})", flush=True)
    return ok


def bg(*args: str, expect_fail: bool = False, title: str | None = None) -> bool:
    return step(title or f"bluegreen {' '.join(args)}", "deploy.bluegreen", *args, expect_fail=expect_fail)


def teardown() -> None:
    subprocess.run([sys.executable, "-m", "deploy.bluegreen", "down"], cwd=PROJECT, capture_output=True)


def reset() -> None:
    for rel in RUNTIME_DIRS:
        shutil.rmtree(PROJECT / rel, ignore_errors=True)
    for rel in RUNTIME_FILES:
        (PROJECT / rel).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the blue-green (Docker + nginx) deployment and incident drill end to end")
    parser.add_argument("--inject", choices=["A", "B", "C", "D"], help="inject a production defect after cutover and roll back")
    parser.add_argument("--keep-running", action="store_true", help="leave the blue, green and nginx containers running")
    args = parser.parse_args()

    if not (PROJECT / "data" / "warehouse" / "settlement.duckdb").exists():
        print("warehouse not found: run `python scripts/generate_sample_data.py` and `python -m src.pipeline.run_pipeline` first")
        return 2
    if shutil.which("docker") is None:
        print("docker not found: install Docker Desktop and make sure it is running")
        return 2
    teardown()
    reset()

    results = []
    try:
        results.append(bg("release", "v1", "--force"))
        results.append(bg("release", "v2", "--force"))
        results.append(bg("start", "blue", "--release", "v1"))
        results.append(bg("lb", "start"))
        results.append(bg("baseline"))
        results.append(bg("start", "green", "--release", "v2"))
        results.append(bg("precheck", "green"))
        results.append(bg("cutover", "green"))

        if args.inject:
            time.sleep(1)
            results.append(step(f"inject defect {args.inject}", "incident.inject_defect", "--release", "v2", "--defect", args.inject))
            results.append(bg("restart", "green"))
            bg("note", f"KPI anomaly check started after defect {args.inject} reached production")
            results.append(step("detect: reconciliation gate against production (must FAIL)", "scripts.reconciliation_gate",
                                "--api-url", LB_URL, "--baseline", "evidence/baseline_kpis.json",
                                "--out", "evidence/runs/incident_detection.json", expect_fail=True))
            bg("status")
            bg("note", "Decision: rollback to blue v1.0.0 - live KPI disagrees with the approved warehouse")
            results.append(bg("rollback", "--reason", f"drill defect {args.inject}"))

        results.append(step("collect evidence", "scripts.collect_evidence"))
        bg("timeline")
    finally:
        if not args.keep_running:
            teardown()

    passed = all(results)
    print(f"\nDRILL {'PASSED' if passed else 'FAILED'}: {sum(results)}/{len(results)} steps as expected")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
