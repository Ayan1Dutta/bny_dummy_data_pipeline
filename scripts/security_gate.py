from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path

from src.config import PLACEHOLDER_SECRETS, utcnow

ROOT = Path(__file__).resolve().parents[1]
SCAN_EXCLUDE_DIRS = {".git", ".venv", "venv", ".cache", "data", "releases", "evidence", "__pycache__",
                     ".pytest_cache", ".ruff_cache", "node_modules", "deploy/logs"}
RUNTIME_FILES = {"deploy/state.json", "dq_report.json"}
TEXT_SUFFIXES = {".py", ".sql", ".yml", ".yaml", ".json", ".md", ".txt", ".ini", ".toml", ".cfg", ".env",
                 ".html", ".js", ".sh", ".tf", ".conf", ""}
SECRET_PATTERNS = {
    "aws_access_key_id": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "password_in_connection_string": re.compile(r"\b[a-z][a-z0-9+]*://[^\s:/@'\"]+:([^\s@'\"$]{3,})@"),
    "credential_assignment": re.compile(
        r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|db[_-]?password)\b"
        r"\s*[:=]\s*['\"]?([^\s'\"$,)}{]{8,})"
    ),
}
SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "MODERATE": 2, "LOW": 1, "UNKNOWN": 5}


def _run(cmd: list[str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def sast(triage_path: Path) -> dict:
    proc = _run([sys.executable, "-m", "bandit", "-r", "src", "-f", "json", "-q"])
    if not proc.stdout.strip():
        return {"status": "ERROR", "blocking": True, "detail": "bandit did not run: " + proc.stderr[-300:]}
    results = json.loads(proc.stdout)["results"]
    triage = json.loads(triage_path.read_text(encoding="utf-8"))
    allowed = {(f["file"], f["rule"]): f for f in triage["findings"]}
    counts = Counter((r["filename"].replace("\\", "/"), r["test_id"]) for r in results if r["issue_severity"] == "MEDIUM")

    blocking = []
    for r in results:
        path = r["filename"].replace("\\", "/")
        if r["issue_severity"] == "HIGH":
            blocking.append(f"HIGH {r['test_id']} {path}:{r['line_number']} {r['issue_text']}")
        if r["test_id"] == "B608" and path.startswith("src/api/"):
            blocking.append(f"B608 in API layer {path}:{r['line_number']}")
    untriaged = []
    for (path, rule), n in counts.items():
        entry = allowed.get((path, rule))
        if entry is None:
            untriaged.append(f"{path} {rule} x{n} (not triaged)")
        elif n > entry["max_count"]:
            untriaged.append(f"{path} {rule} x{n} (triaged max {entry['max_count']}) - new finding")
    blocking.extend(untriaged)
    by_sev = Counter(r["issue_severity"] for r in results)
    return {
        "status": "FAIL" if blocking else "PASS",
        "blocking": bool(blocking),
        "tool": "bandit",
        "counts_by_severity": dict(by_sev),
        "triaged": [f"{p} {r} x{n}" for (p, r), n in counts.items() if (p, r) in allowed and n <= allowed[(p, r)]["max_count"]],
        "blocking_findings": blocking,
        "findings": [
            {"file": r["filename"].replace("\\", "/"), "line": r["line_number"], "rule": r["test_id"],
             "severity": r["issue_severity"], "confidence": r["issue_confidence"], "text": r["issue_text"]}
            for r in results
        ],
    }


def _scannable_files() -> list[Path]:
    files = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in RUNTIME_FILES:
            continue
        if not path.is_file() or any(rel == d or rel.startswith(d + "/") or f"/{d}/" in f"/{rel}" for d in SCAN_EXCLUDE_DIRS):
            continue
        named = path.name in {"Dockerfile", "Jenkinsfile", "Makefile", ".gitlab-ci.yml"}
        if (path.suffix.lower() in TEXT_SUFFIXES or named) and path.stat().st_size < 2_000_000:
            files.append(path)
    return files


def scan_lines(rel: str, lines: list[str]) -> list[dict]:
    hits = []
    is_python = rel.endswith(".py")
    for number, line in enumerate(lines, start=1):
        for name, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(line):
                value = match.group(match.lastindex) if match.lastindex else match.group(0)
                value = value.lstrip("-")
                if value in PLACEHOLDER_SECRETS or value.startswith(("os.", "settings.", "env", "${", "<")):
                    continue
                if name == "credential_assignment":
                    quoted = line[match.start(2) - 1: match.start(2)] in {"'", '"'}
                    if is_python and not quoted:
                        continue
                    if "/" in value or "{" in value:
                        continue
                hits.append({"file": rel, "line": number, "rule": name, "evidence": value[:4] + "****"})
    return hits


def secrets_scan(allowlist_path: Path) -> dict:
    hits = []
    for path in _scannable_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel == "scripts/security_gate.py":
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        hits.extend(scan_lines(rel, lines))

    detect_secrets = {"status": "SKIPPED", "detail": "detect-secrets not installed"}
    if shutil.which("detect-secrets") or _run([sys.executable, "-m", "detect_secrets", "--version"]).returncode == 0:
        proc = _run([sys.executable, "-m", "detect_secrets", "scan", "--all-files",
                     "--exclude-files", r"(^|/)(data|releases|evidence|\.venv|venv|deploy/logs|\.pytest_cache|\.ruff_cache|\.cache|\.git)/|\.duckdb$|deploy/state\.json$|dq_report\.json$"])
        try:
            found = json.loads(proc.stdout)["results"]
            for file, items in found.items():
                for item in items:
                    hits.append({"file": file, "line": item["line_number"], "rule": f"detect-secrets:{item['type']}", "evidence": "****"})
            detect_secrets = {"status": "RAN", "files_flagged": len(found)}
        except (ValueError, KeyError):
            detect_secrets = {"status": "ERROR", "detail": proc.stderr[-300:]}
    allow = json.loads(allowlist_path.read_text(encoding="utf-8")).get("entries", [])
    allowlisted, blocking = [], []
    per_file = Counter(h["file"] for h in hits)
    for h in hits:
        entry = next((e for e in allow if e["file"] == h["file"] and h["rule"] in e["rules"]), None)
        if entry and per_file[h["file"]] <= entry["max_count"]:
            allowlisted.append({**h, "reason": entry["reason"]})
        else:
            blocking.append(h)
    return {
        "status": "FAIL" if blocking else "PASS",
        "blocking": bool(blocking),
        "allowlisted": allowlisted,
        "tools": ["built-in pattern scan", "detect-secrets"],
        "detect_secrets": detect_secrets,
        "files_scanned": len(_scannable_files()),
        "findings": blocking,
    }


def _lookup_severity(vuln: dict, cache: dict) -> str:
    ids = [vuln["id"], *vuln.get("aliases", [])]
    for ident in ids:
        if ident in cache:
            return cache[ident]
    for ident in ids:
        try:
            if ident.startswith("GHSA-"):
                req = urllib.request.Request(f"https://api.github.com/advisories/{ident}",
                                             headers={"Accept": "application/vnd.github+json", "User-Agent": "sca-gate"})
                data = json.load(urllib.request.urlopen(req, timeout=6))
                severity = (data.get("severity") or "UNKNOWN").upper()
            else:
                data = json.load(urllib.request.urlopen(f"https://api.osv.dev/v1/vulns/{ident}", timeout=6))
                severity = ((data.get("database_specific") or {}).get("severity") or "UNKNOWN").upper()
            severity = "MEDIUM" if severity == "MODERATE" else severity
            if severity != "UNKNOWN":
                cache.update(dict.fromkeys(ids, severity))
                return severity
        except Exception:
            continue
    return "UNKNOWN"


def _active_waiver(waivers: list[dict], package: str, vuln_ids: list[str]) -> dict | None:
    today = date.today().isoformat()
    for w in waivers:
        if w.get("package", "").lower() == package.lower() and w.get("vuln_id") in vuln_ids and w.get("expires", "") >= today:
            return w
    return None


def sca(requirements: Path, waivers_path: Path) -> dict:
    proc = _run([sys.executable, "-m", "pip_audit", "-r", str(requirements), "--format", "json", "--progress-spinner", "off"])
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {"status": "ERROR", "blocking": True, "detail": "pip-audit did not return JSON: " + proc.stderr[-400:]}
    dependencies = data.get("dependencies", data)
    waivers = json.loads(waivers_path.read_text(encoding="utf-8")).get("waivers", [])
    cache: dict = {}
    findings, seen = [], set()
    for dep in dependencies:
        for v in dep.get("vulns", []):
            key = (dep["name"], v["id"])
            if key in seen:
                continue
            seen.add(key)
            severity = _lookup_severity(v, cache)
            waiver = _active_waiver(waivers, dep["name"], [v["id"], *v.get("aliases", [])])
            blocks = SEVERITY_RANK.get(severity, 5) >= 3 and waiver is None
            fixes = v.get("fix_versions") or []
            findings.append({
                "package": dep["name"],
                "installed": dep.get("version"),
                "vulnerability": v["id"],
                "aliases": v.get("aliases", []),
                "severity": severity,
                "blocks_production": blocks,
                "waiver": waiver,
                "remediation": f"upgrade {dep['name']} to >= {sorted(fixes)[0]}" if fixes else "no fix published: replace the package, mitigate, or raise a time-boxed waiver",
            })
    blocking = [f for f in findings if f["blocks_production"]]
    return {
        "status": "FAIL" if blocking else "PASS",
        "blocking": bool(blocking),
        "tool": "pip-audit",
        "requirements": str(requirements),
        "dependencies_scanned": len(dependencies),
        "vulnerable_packages": sorted({f["package"] for f in findings}),
        "counts_by_severity": dict(Counter(f["severity"] for f in findings)),
        "findings": findings,
    }


def container_scan(image: str | None, required: bool) -> dict:
    if not image:
        return {"status": "FAIL" if required else "SKIPPED", "blocking": required, "detail": "no --image given"}
    if not shutil.which("trivy"):
        return {"status": "FAIL" if required else "SKIPPED", "blocking": required,
                "detail": "trivy not installed; the GitLab and Jenkins pipelines run this scan"}
    proc = _run(["trivy", "image", "--quiet", "--format", "json", "--severity", "HIGH,CRITICAL", "--ignore-unfixed", image])
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {"status": "ERROR", "blocking": True, "detail": proc.stderr[-400:]}
    vulns = [v for r in data.get("Results", []) for v in (r.get("Vulnerabilities") or [])]
    counts = Counter(v.get("Severity") for v in vulns)
    return {
        "status": "FAIL" if counts.get("CRITICAL") else "PASS",
        "blocking": bool(counts.get("CRITICAL")),
        "tool": "trivy",
        "image": image,
        "counts_by_severity": dict(counts),
        "policy": "CRITICAL with a published fix blocks; HIGH is reported for remediation",
        "critical": [{"id": v.get("VulnerabilityID"), "package": v.get("PkgName"), "fixed_in": v.get("FixedVersion")}
                     for v in vulns if v.get("Severity") == "CRITICAL"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Security gate: SAST, secret scan, SCA, container scan")
    parser.add_argument("--out", default="evidence/02_security_results")
    parser.add_argument("--requirements", default="requirements.txt")
    parser.add_argument("--triage", default="security/sast_triage.json")
    parser.add_argument("--waivers", default="security/sca_waivers.json")
    parser.add_argument("--secrets-allowlist", default="security/secrets_allowlist.json")
    parser.add_argument("--image", default=None, help="Docker image to scan with trivy")
    parser.add_argument("--require-container-scan", action="store_true")
    parser.add_argument("--checks", default="sast,secrets,sca,container", help="comma separated subset to run")
    args = parser.parse_args()
    selected = {c.strip() for c in args.checks.split(",") if c.strip()}

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runners = {
        "sast": lambda: sast(ROOT / args.triage),
        "secrets": lambda: secrets_scan(ROOT / args.secrets_allowlist),
        "sca": lambda: sca(ROOT / args.requirements, ROOT / args.waivers),
        "container": lambda: container_scan(args.image, args.require_container_scan),
    }
    checks = {name: run() for name, run in runners.items() if name in selected}
    for name, result in checks.items():
        (out / f"{name}.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    critical = sum(1 for r in checks.values() if r["blocking"])
    summary = {
        "generated_at": utcnow().isoformat(),
        "results": {k: v["status"] for k, v in checks.items()},
        "blocking_checks": [k for k, v in checks.items() if v["blocking"]],
        "critical_findings": critical,
        "passed": critical == 0,
    }
    summary_name = "summary.json" if selected >= {"sast", "secrets", "sca", "container"} else f"summary_{'_'.join(sorted(selected))}.json"
    (out / summary_name).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print_summary(checks, summary)
    return 0 if summary["passed"] else 1


def print_summary(checks: dict, summary: dict) -> None:
    s = checks.get("sast")
    if s is None:
        s = {"status": "SKIPPED"}
    print(f"SAST       {s['status']:<8} severities={s.get('counts_by_severity')} triaged={len(s.get('triaged', []))} blocking={len(s.get('blocking_findings', []))}")
    for b in s.get("blocking_findings", []):
        print(f"           blocking: {b}")
    sec = checks.get("secrets") or {"status": "SKIPPED", "files_scanned": 0, "findings": [], "allowlisted": [], "detect_secrets": {"status": "-"}}
    print(f"SECRETS    {sec['status']:<8} files_scanned={sec['files_scanned']} blocking={len(sec['findings'])} allowlisted={len(sec['allowlisted'])} detect-secrets={sec['detect_secrets']['status']}")
    for h in sec["findings"][:20]:
        print(f"           {h['file']}:{h['line']} {h['rule']} {h['evidence']}")
    c = checks.get("sca") or {"status": "SKIPPED"}
    print(f"SCA        {c['status']:<8} scanned={c.get('dependencies_scanned')} vulnerable={c.get('vulnerable_packages')} severities={c.get('counts_by_severity')}")
    for f in c.get("findings", [])[:25]:
        flag = "BLOCKS" if f["blocks_production"] else ("WAIVED" if f["waiver"] else "report")
        print(f"           {f['package']} {f['installed']} {f['vulnerability']} {f['severity']:<8} {flag:<7} -> {f['remediation']}")
    k = checks.get("container") or {"status": "SKIPPED", "detail": "not selected"}
    print(f"CONTAINER  {k['status']:<8} {k.get('counts_by_severity') or k.get('detail')}")
    print(f"SECURITY GATE: {'PASS' if summary['passed'] else 'FAIL'}  (blocking checks: {summary['blocking_checks'] or 'none'})")


if __name__ == "__main__":
    sys.exit(main())
