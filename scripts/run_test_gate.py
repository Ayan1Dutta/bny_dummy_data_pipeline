from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from src.config import utcnow

STAGES = ["unit", "data_model", "pipeline", "api", "business", "security"]


def run_stage(stage: str, out_dir: Path) -> dict:
    junit = out_dir / f"junit_{stage}.xml"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-m", stage, "-q", "-p", "no:logging", f"--junitxml={junit}"],
        capture_output=True,
        text=True,
    )
    (out_dir / f"{stage}.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    failed_tests = []
    if junit.exists():
        root = ET.parse(junit).getroot()
        suite = root if root.tag == "testsuite" else root.find("testsuite")
        for key in ("tests", "failures", "errors", "skipped"):
            counts[key] = int(suite.get(key, 0))
        counts["time"] = float(suite.get("time", 0))
        for case in suite.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failed_tests.append(f"{case.get('classname')}::{case.get('name')}")
    passed = proc.returncode == 0 and counts["tests"] > 0
    return {"stage": stage, "passed": passed, **counts, "failed_tests": failed_tests}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the production test gate stage by stage")
    parser.add_argument("--out", default="evidence/01_test_results")
    parser.add_argument("--continue-on-failure", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    print(f"{'stage':<12}{'result':<8}{'tests':>6}{'failed':>8}{'seconds':>9}")
    for stage in STAGES:
        r = run_stage(stage, out_dir)
        results.append(r)
        print(f"{stage:<12}{'PASS' if r['passed'] else 'FAIL':<8}{r['tests']:>6}{r['failures'] + r['errors']:>8}{r['time']:>9.2f}")
        for name in r["failed_tests"]:
            print(f"    failed: {name}")
        if not r["passed"] and not args.continue_on_failure:
            print(f"Gate stopped at stage '{stage}'.")
            break

    total = sum(r["tests"] for r in results)
    failed = sum(r["failures"] + r["errors"] for r in results)
    gate_passed = len(results) == len(STAGES) and all(r["passed"] for r in results)
    summary = {
        "generated_at": utcnow().isoformat(),
        "stages": results,
        "total_tests": total,
        "failed_tests": failed,
        "pass_rate_pct": round(100.0 * (total - failed) / total, 2) if total else 0.0,
        "passed": gate_passed,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"TOTAL {total} tests, {failed} failed, pass rate {summary['pass_rate_pct']}%")
    print("TEST GATE:", "PASS" if gate_passed else "FAIL")
    return 0 if gate_passed else 1


if __name__ == "__main__":
    sys.exit(main())
