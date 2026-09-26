from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

import httpx

PROJECT = Path(__file__).resolve().parents[1]
STATE_PATH = PROJECT / "deploy" / "state.json"
ENV_DIR = PROJECT / "deploy" / "env"
EVIDENCE = PROJECT / "evidence"
COMPOSE_FILE = PROJECT / "docker-compose.bluegreen.yml"
HOST = os.getenv("BLUEGREEN_HOST", "127.0.0.1")
PORTS = {"blue": 8001, "green": 8002}
LB_PORT = 8000
IMAGE_REPO = os.getenv("IMAGE_REPO", "settlement-api")
CONTAINER_WAREHOUSE_DIR = "/app/data/warehouse"
DEFAULT_WAREHOUSE = f"{CONTAINER_WAREHOUSE_DIR}/settlement.duckdb"
WINDOW = ("2026-09-01", "2026-09-07")

sys.path.insert(0, str(PROJECT))
from scripts import reconciliation_gate, smoke_test  # noqa: E402
from src.config import settings  # noqa: E402

APPROVED_WAREHOUSE = str(Path(os.getenv("APPROVED_WAREHOUSE", settings.warehouse_path)).resolve())

ACTIVE_CONF_TEMPLATE = """upstream active_backend {{
    server api-{color}:8000;
    keepalive 16;
}}

map $host $active_color {{
    default "{color}";
}}
"""

SWITCH_SCRIPT = (
    "set -e; d=/etc/nginx/bluegreen; cat > $d/active.conf.new; "
    "cp $d/active.conf $d/active.conf.bak; mv $d/active.conf.new $d/active.conf; "
    "if nginx -t 2>/tmp/nginx-test.log; then nginx -s reload; "
    "else cp $d/active.conf.bak $d/active.conf; cat /tmp/nginx-test.log >&2; exit 1; fi"
)


class DeployError(Exception):
    pass


def render_active_conf(color: str) -> str:
    if color not in PORTS:
        raise ValueError(f"unknown colour {color}")
    return ACTIVE_CONF_TEMPLATE.format(color=color)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"active": None, "previous_active": None, "releases": {}, "colors": {}, "prechecks": {},
            "seeded": False, "history": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def record(state: dict, event: str, detail: str = "") -> None:
    state["history"].append({"ts": now(), "event": event, "detail": detail})
    print(f"[{state['history'][-1]['ts']}] {event}: {detail}")


def run(cmd: list[str], stdin: str | None = None, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, cwd=PROJECT)
    if check and proc.returncode != 0:
        raise DeployError(f"{' '.join(cmd[:4])} ... failed:\n{(proc.stderr or proc.stdout).strip()[-1500:]}")
    return proc


def compose_env(state: dict) -> dict:
    tags = {c: state["colors"].get(c, {}).get("release") for c in PORTS}
    return {**os.environ, "BLUE_TAG": tags["blue"] or "v1", "GREEN_TAG": tags["green"] or "v2"}


def compose(state: dict, *args: str, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return run(["docker", "compose", "-f", str(COMPOSE_FILE), *args], stdin=stdin, env=compose_env(state), check=check)


def image_id(ref: str) -> str | None:
    proc = run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], check=False)
    return proc.stdout.strip() or None


def container_image_id(state: dict, color: str) -> str | None:
    cid = compose(state, "ps", "-q", f"api-{color}", check=False).stdout.strip()
    if not cid:
        return None
    return run(["docker", "inspect", "--format", "{{.Image}}", cid], check=False).stdout.strip() or None


def env_file(color: str) -> Path:
    return ENV_DIR / f"{color}.env"


def ensure_env_files() -> None:
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    for color in PORTS:
        if not env_file(color).exists():
            env_file(color).write_text(f"WAREHOUSE_PATH={DEFAULT_WAREHOUSE}\n", encoding="utf-8")


def read_env(color: str) -> dict:
    values = {}
    for line in env_file(color).read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def config_fingerprint(color: str) -> str:
    return hashlib.sha256(env_file(color).read_bytes()).hexdigest()


def short(value: str | None) -> str:
    return (value or "-").replace("sha256:", "")[:12]


def get_json(port: int, path: str, timeout: float = 3) -> dict | None:
    try:
        r = httpx.get(f"http://{HOST}:{port}{path}", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def wait_for(port: int, path: str = "/health", seconds: float = 40) -> dict | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        body = get_json(port, path)
        if body:
            return body
        time.sleep(0.5)
    return None


def api_client(port: int) -> httpx.Client:
    return httpx.Client(base_url=f"http://{HOST}:{port}", timeout=15, headers={"X-API-Key": settings.api_key})


def lb_running(state: dict) -> bool:
    return bool(compose(state, "ps", "-q", "--status", "running", "lb", check=False).stdout.strip())


def lb_active() -> str | None:
    body = get_json(LB_PORT, "/lb/status")
    return body.get("active") if body else None


def switch_traffic(state: dict, color: str) -> None:
    compose(state, "exec", "-T", "lb", "sh", "-c", SWITCH_SCRIPT, stdin=render_active_conf(color))
    deadline = time.time() + 10
    while time.time() < deadline:
        if lb_active() == color:
            return
        time.sleep(0.2)
    raise DeployError(f"nginx reloaded but /lb/status does not report {color}")


def reload_lb(state: dict) -> None:
    if lb_running(state):
        compose(state, "exec", "-T", "lb", "nginx", "-s", "reload", check=False)


def integrity(state: dict, color: str) -> dict:
    info = state["colors"].get(color, {})
    running = container_image_id(state, color)
    approved = state["releases"].get(info.get("release"), {}).get("image_id")
    config_now = config_fingerprint(color) if env_file(color).exists() else None
    return {
        "running_image_id": running,
        "approved_image_id": approved,
        "image_ok": bool(running) and running == approved,
        "config_fingerprint": config_now,
        "approved_config_fingerprint": info.get("approved_config_fingerprint"),
        "config_ok": config_now == info.get("approved_config_fingerprint"),
    }


def cmd_release(args) -> int:
    state = load_state()
    if args.name in state["releases"] and not args.force:
        print(f"release {args.name} already exists; releases are immutable (use --force only to rebuild a drill release)")
        return 1
    version = args.version or (f"{args.name[1:]}.0.0" if re.fullmatch(r"v\d+", args.name) else args.name)
    tests = {"passed": None, "detail": "skipped by operator"}
    if not args.skip_tests:
        out = run_dir(f"release_{args.name}")
        proc = subprocess.run([sys.executable, "-m", "scripts.run_test_gate", "--out", str(out)], cwd=PROJECT,
                              capture_output=True, text=True)
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8")) if (out / "summary.json").exists() else {}
        tests = {"passed": proc.returncode == 0,
                 "detail": f"{summary.get('total_tests', 0) - summary.get('failed_tests', 0)}/{summary.get('total_tests', 0)} passed",
                 "report": str((out / "summary.json").relative_to(PROJECT))}
        if not tests["passed"]:
            print(f"refusing to build {args.name}: test gate failed ({tests['detail']})")
            return 1
    ref = f"{IMAGE_REPO}:{args.name}"
    build = ["docker", "build", "-t", ref, "--build-arg", f"APP_VERSION={version}"]
    if os.getenv("PYTHON_IMAGE"):
        build += ["--build-arg", f"PYTHON_IMAGE={os.environ['PYTHON_IMAGE']}"]
    print(f"building {ref} (version {version}) ...")
    run([*build, str(PROJECT)])
    state["releases"][args.name] = {"image": ref, "image_id": image_id(ref), "version": version,
                                    "built_at": now(), "tests": tests}
    record(state, "release_built", f"{ref} version={version} image_id={short(image_id(ref))} tests={tests['detail']}")
    save_state(state)
    return 0


def seed_warehouse(state: dict, color: str) -> None:
    src = Path(APPROVED_WAREHOUSE)
    if not src.exists():
        raise DeployError(f"approved warehouse not found at {src}; run the pipeline first")
    compose(state, "cp", str(src), f"api-{color}:{DEFAULT_WAREHOUSE}")
    state["seeded"] = True
    record(state, "warehouse_seeded", f"{src.name} copied into the shared warehouse volume")


def start_color(state: dict, color: str, release: str) -> int:
    if release not in state["releases"]:
        print(f"release {release} not found; build it with: python -m deploy.bluegreen release {release}")
        return 1
    ensure_env_files()
    previous = state["colors"].get(color, {})
    state["colors"][color] = {**previous, "release": release, "port": PORTS[color]}
    if previous.get("release") != release or not previous.get("approved_config_fingerprint"):
        state["colors"][color]["approved_config_fingerprint"] = config_fingerprint(color)
    compose(state, "up", "-d", "--no-deps", "--force-recreate", f"api-{color}")
    if not state.get("seeded"):
        seed_warehouse(state, color)
    info = wait_for(PORTS[color])
    if not info:
        print(f"{color} did not become healthy; recent logs:")
        print(compose(state, "logs", "--tail", "25", f"api-{color}", check=False).stdout)
        return 1
    check = integrity(state, color)
    state["colors"][color].update({"version": info.get("version"), "image": state["releases"][release]["image"],
                                   "image_id": check["running_image_id"], "started_at": now(),
                                   "warehouse": read_env(color).get("WAREHOUSE_PATH", DEFAULT_WAREHOUSE),
                                   "tampered": not (check["image_ok"] and check["config_ok"])})
    record(state, "started", f"{color} release={release} version={info.get('version')} port={PORTS[color]} "
                             f"image_id={short(check['running_image_id'])} "
                             f"warehouse={Path(state['colors'][color]['warehouse']).name}")
    if not check["image_ok"]:
        record(state, "artifact_integrity_warning",
               f"{color} runs image {short(check['running_image_id'])} but release {release} was approved as "
               f"{short(check['approved_image_id'])}: the image changed after it was built and tested")
    if not check["config_ok"]:
        record(state, "config_integrity_warning",
               f"{color} configuration changed after approval ({env_file(color).relative_to(PROJECT).as_posix()})")
    if not state.get("active"):
        state["active"] = color
        record(state, "traffic", f"initial deployment: 100% -> {color}")
    elif state.get("active") == color:
        reload_lb(state)
    return 0


def cmd_start(args) -> int:
    state = load_state()
    code = start_color(state, args.color, args.release)
    save_state(state)
    return code


def cmd_restart(args) -> int:
    state = load_state()
    info = state["colors"].get(args.color)
    if not info:
        print(f"{args.color} has never been started")
        return 1
    code = start_color(state, args.color, info["release"])
    save_state(state)
    return code


def cmd_stop(args) -> int:
    state = load_state()
    if state.get("active") == args.color and not args.force:
        print(f"{args.color} is receiving traffic; cut over or roll back first (or --force)")
        return 1
    compose(state, "stop", f"api-{args.color}")
    record(state, "stopped", args.color)
    save_state(state)
    return 0


def cmd_lb(args) -> int:
    state = load_state()
    if args.action == "stop":
        compose(state, "stop", "lb")
        record(state, "lb_stopped", "")
        save_state(state)
        return 0
    active = state.get("active")
    if not active:
        print("start a colour first: python -m deploy.bluegreen start blue --release v1")
        return 1
    ensure_env_files()
    compose(state, "build", "lb")
    compose(state, "up", "-d", "--no-deps", "lb")
    if not wait_for(LB_PORT, "/lb/status", 20):
        print(compose(state, "logs", "--tail", "25", "lb", check=False).stdout)
        return 1
    if lb_active() != active:
        switch_traffic(state, active)
    record(state, "lb_started", f"nginx on http://{HOST}:{LB_PORT} -> upstream api-{active}:8000")
    save_state(state)
    return 0


def run_dir(label: str) -> Path:
    path = EVIDENCE / "runs" / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{label}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_baseline() -> dict | None:
    path = EVIDENCE / "baseline_kpis.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def verify(port: int, label: str, out: Path) -> tuple[bool, dict, dict]:
    baseline = load_baseline()
    expected = (baseline or reconciliation_gate.expected_kpis(APPROVED_WAREHOUSE, *WINDOW))["platform"]
    with api_client(port) as client:
        smoke = smoke_test.run_smoke(client, *WINDOW, expected=expected)
        recon = reconciliation_gate.run(APPROVED_WAREHOUSE, *WINDOW, client=client, baseline=baseline)
    (out / "smoke_test.json").write_text(json.dumps(smoke, indent=2, default=str), encoding="utf-8")
    (out / "kpi_reconciliation.json").write_text(json.dumps(recon, indent=2, default=str), encoding="utf-8")
    print(f"--- {label}: smoke test")
    smoke_test.print_report(smoke, f"http://{HOST}:{port}")
    print(f"--- {label}: KPI reconciliation against approved warehouse {Path(APPROVED_WAREHOUSE).name}")
    reconciliation_gate.print_report(recon)
    return smoke["passed"] and recon["passed"], smoke, recon


def cmd_baseline(args) -> int:
    state = load_state()
    expected = reconciliation_gate.expected_kpis(APPROVED_WAREHOUSE, *WINDOW)
    active = state.get("active")
    if active:
        with api_client(PORTS[active]) as client:
            observed = client.get("/api/v1/settlement-summary", params={"start_date": WINDOW[0], "end_date": WINDOW[1]}).json()
        checks = reconciliation_gate.compare_summary(observed, expected["platform"], "baseline_source")
        if any(c["status"] == "FAIL" for c in checks):
            print("active production does not match the approved warehouse; refusing to record it as a baseline")
            for c in checks:
                print(f"  [{c['status']}] {c['check']}: {c['detail']}")
            return 1
        expected["captured_from"] = f"{active} version={state['colors'][active]['version']}"
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / "baseline_kpis.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
    p = expected["platform"]
    record(state, "baseline_captured", f"settlement_rate={p['settlement_rate']}% sla_rate={p['sla_rate']}% "
                                       f"settled={p['settled_amount']:,.2f} from {expected.get('captured_from', 'approved warehouse')}")
    save_state(state)
    return 0


def cmd_precheck(args) -> int:
    state = load_state()
    info = state["colors"].get(args.color)
    if not info:
        print(f"{args.color} is not running")
        return 1
    port = PORTS[args.color]
    out = run_dir(f"precheck_{args.color}")
    release = state["releases"].get(info["release"], {})
    check = integrity(state, args.color)
    checks = [
        ("artifact_integrity", check["image_ok"],
         f"running {short(check['running_image_id'])} approved {short(check['approved_image_id'])}"),
        ("configuration_integrity", check["config_ok"],
         f"warehouse={Path(read_env(args.color).get('WAREHOUSE_PATH', DEFAULT_WAREHOUSE)).name}"),
    ]
    health = get_json(port, "/health")
    checks.append(("application_health", bool(health), f"version={health.get('version') if health else None}"))
    ready = get_json(port, "/ready", 5) or {}
    checks.append(("database_connectivity", ready.get("warehouse_reachable") is True, f"gold_rows={ready.get('gold_rows')}"))
    checks.append(("pipeline_health", ready.get("last_run_status") == "SUCCESS",
                   f"last_batch={ready.get('last_batch_id')} status={ready.get('last_run_status')}"))
    tests = release.get("tests", {})
    checks.append(("test_suite", tests.get("passed") is True, f"release {info['release']}: {tests.get('detail', 'no test record')}"))
    ok, smoke, recon = verify(port, f"precheck {args.color}", out)
    checks.append(("smoke_test", smoke["passed"], f"{sum(r['status'] == 'PASS' for r in smoke['results'])}/{len(smoke['results'])} checks"))
    checks.append(("kpi_reconciliation", recon["passed"],
                   f"rate={recon['observed']['settlement_rate']}% expected={recon['expected_from_facts']['settlement_rate']}%"))

    passed = all(c[1] for c in checks)
    report = {"color": args.color, "release": info["release"], "version": info.get("version"),
              "image_id": check["running_image_id"], "config_fingerprint": check["config_fingerprint"], "ts": now(),
              "passed": passed, "checks": [{"check": c[0], "status": "PASS" if c[1] else "FAIL", "detail": c[2]} for c in checks]}
    (out / "precheck.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"=== PRE-CUTOVER CHECKS: {args.color} ({info['release']} v{info.get('version')}) ===")
    for c in report["checks"]:
        print(f"  [{c['status']}] {c['check']}: {c['detail']}")
    state["prechecks"][args.color] = {"passed": passed, "ts": report["ts"], "image_id": check["running_image_id"],
                                      "config_fingerprint": check["config_fingerprint"], "version": info.get("version"),
                                      "report": str((out / "precheck.json").relative_to(PROJECT))}
    record(state, "precheck_passed" if passed else "precheck_failed", f"{args.color} v{info.get('version')}")
    save_state(state)
    return 0 if passed else 1


def precheck_is_fresh(state: dict, color: str) -> bool:
    pre = state["prechecks"].get(color, {})
    if not pre.get("passed"):
        return False
    check = integrity(state, color)
    same = pre.get("image_id") == check["running_image_id"] and pre.get("config_fingerprint") == check["config_fingerprint"]
    age = (datetime.now() - datetime.fromisoformat(pre["ts"])).total_seconds()
    return same and age < 7200


def cmd_cutover(args) -> int:
    state = load_state()
    info = state["colors"].get(args.color)
    if not info or not get_json(PORTS[args.color], "/health"):
        print(f"{args.color} is not healthy; cannot receive traffic")
        return 1
    if not lb_running(state):
        print("load balancer is not running: python -m deploy.bluegreen lb start")
        return 1
    if state.get("active") == args.color:
        print(f"{args.color} is already active")
        return 0
    fresh = precheck_is_fresh(state, args.color)
    if not fresh and not args.force:
        print(f"refusing cutover: no passing pre-cutover check for the exact image and configuration now running "
              f"in {args.color} (within 2 hours)")
        return 1
    previous = state.get("active")
    switch_traffic(state, args.color)
    state["previous_active"], state["active"] = previous, args.color
    prev_version = state["colors"].get(previous, {}).get("version") if previous else None
    record(state, "cutover" if fresh else "cutover_forced",
           f"{previous}(v{prev_version}) -> {args.color}(v{info.get('version')}): nginx upstream -> api-{args.color}")
    save_state(state)
    ok, _, _ = verify(LB_PORT, "post-cutover via load balancer", run_dir("post_cutover"))
    record(state, "post_cutover_verified" if ok else "post_cutover_verification_failed", f"via nginx :{LB_PORT}")
    save_state(state)
    return 0


def cmd_rollback(args) -> int:
    state = load_state()
    current = state.get("active")
    target = state.get("previous_active") or ("blue" if current == "green" else "green")
    info = state["colors"].get(target)
    if not info or not get_json(PORTS[target], "/health"):
        print(f"rollback target {target} is not running and healthy; start it first")
        return 1
    before = None
    try:
        with api_client(LB_PORT) as client:
            r = client.get("/api/v1/settlement-summary", params={"start_date": WINDOW[0], "end_date": WINDOW[1]})
            before = r.json() if r.status_code == 200 else {"http_status": r.status_code}
    except httpx.HTTPError:
        before = {"error": "load balancer unreachable"}
    current_version = state["colors"].get(current, {}).get("version")
    record(state, "rollback_initiated", f"{current}(v{current_version}) -> {target}(v{info.get('version')}) "
                                        f"reason={args.reason or 'not given'}")
    switch_traffic(state, target)
    state["previous_active"], state["active"] = current, target
    record(state, "traffic_restored", f"nginx upstream -> api-{target} v{info.get('version')}")
    save_state(state)

    out = run_dir("rollback")
    ok, smoke, recon = verify(LB_PORT, "after rollback", out)
    record(state, "smoke_tests_passed" if smoke["passed"] else "smoke_tests_failed", f"{target} via nginx")
    record(state, "kpi_reconciled" if recon["passed"] else "kpi_reconciliation_failed",
           f"settlement_rate={recon['observed']['settlement_rate']}% expected={recon['expected_from_facts']['settlement_rate']}%")
    evidence = {"ts": now(), "from": current, "to": target, "reason": args.reason,
                "before": before, "after": smoke.get("summary"), "passed": ok}
    (out / "rollback.json").write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    b = before.get("settlement_rate") if isinstance(before, dict) else None
    a = (smoke.get("summary") or {}).get("settlement_rate")
    print(f"=== ROLLBACK {'VERIFIED' if ok else 'NOT VERIFIED'}: settlement rate before={b}% after={a}% ===")
    save_state(state)
    return 0 if ok else 1


def cmd_seed(args) -> int:
    state = load_state()
    running = [c for c in PORTS if container_image_id(state, c)]
    if not running:
        print("start a colour first; the warehouse is copied through a running container")
        return 1
    seed_warehouse(state, running[0])
    for color in running:
        start_color(state, color, state["colors"][color]["release"])
    save_state(state)
    return 0


def cmd_note(args) -> int:
    state = load_state()
    record(state, "note", " ".join(args.text))
    save_state(state)
    return 0


def cmd_status(args) -> int:
    state = load_state()
    active_lb = lb_active()
    print(f"load balancer: nginx http://{HOST}:{LB_PORT}  "
          f"{'UP' if active_lb else 'DOWN'}  upstream -> {('api-' + active_lb) if active_lb else '-'}  "
          f"(state file says active={state.get('active')}, previous={state.get('previous_active')})")
    for color in PORTS:
        info = state["colors"].get(color)
        if not info:
            print(f"  {color:<6} not deployed")
            continue
        health = get_json(PORTS[color], "/health")
        check = integrity(state, color)
        pre = state["prechecks"].get(color, {})
        pre_label = "-" if not pre else ("PASS" if precheck_is_fresh(state, color) else ("STALE" if pre.get("passed") else "FAIL"))
        flags = [] if check["image_ok"] else ["IMAGE CHANGED"]
        flags += [] if check["config_ok"] else ["CONFIG CHANGED"]
        print(f"  {color:<6} release={info['release']:<4} version={str(info.get('version')):<7} port={PORTS[color]} "
              f"health={'UP' if health else 'DOWN'} image={short(check['running_image_id'])} "
              f"warehouse={Path(read_env(color).get('WAREHOUSE_PATH', DEFAULT_WAREHOUSE)).name} precheck={pre_label} "
              f"{' '.join(flags)} {'<-- LIVE' if active_lb == color else ''}".rstrip())
    return 0


def cmd_timeline(args) -> int:
    for e in load_state().get("history", []):
        print(f"{e['ts']}  {e['event']:<30} {e['detail']}")
    return 0


def cmd_logs(args) -> int:
    state = load_state()
    service = "lb" if args.service == "lb" else f"api-{args.service}"
    print(compose(state, "logs", "--tail", str(args.tail), service, check=False).stdout)
    return 0


def export_source(image: str, dest: Path) -> None:
    cid = run(["docker", "create", image]).stdout.strip()
    try:
        data = subprocess.run(["docker", "cp", f"{cid}:/app/src", "-"], capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(dest, filter="data")
    finally:
        run(["docker", "rm", "-f", cid], check=False)


def cmd_diff(args) -> int:
    refs = [f"{IMAGE_REPO}:{args.old}", f"{IMAGE_REPO}:{args.new}"]
    with tempfile.TemporaryDirectory() as tmp:
        dirs = []
        for i, ref in enumerate(refs):
            dest = Path(tmp) / str(i)
            dest.mkdir()
            export_source(ref, dest)
            dirs.append(dest / "src")
        a, b = dirs
        changed = 0
        files = {p.relative_to(a) for p in a.rglob("*.py")} | {p.relative_to(b) for p in b.rglob("*.py")}
        for path in sorted(files):
            old = (a / path).read_text(encoding="utf-8").splitlines() if (a / path).exists() else []
            new = (b / path).read_text(encoding="utf-8").splitlines() if (b / path).exists() else []
            if old != new:
                changed += 1
                sys.stdout.writelines(line + "\n" for line in difflib.unified_diff(
                    old, new, f"{refs[0]}:/app/src/{path.as_posix()}", f"{refs[1]}:/app/src/{path.as_posix()}", lineterm="", n=2))
    print(f"--- {changed} file(s) differ between {refs[0]} and {refs[1]}")
    return 0


def cmd_down(args) -> int:
    state = load_state()
    compose(state, "down", "--volumes", "--remove-orphans", check=False)
    state.update({"active": None, "previous_active": None, "colors": {}, "prechecks": {}, "seeded": False})
    record(state, "environment_removed", "containers and volumes removed; release images kept")
    save_state(state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Blue-green deployment controller (Docker + nginx)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("release", help="run the test gate, then build an immutable image settlement-api:<name>")
    p.add_argument("name")
    p.add_argument("--version")
    p.add_argument("--skip-tests", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_release)
    p = sub.add_parser("start", help="start or replace a colour's container")
    p.add_argument("color", choices=list(PORTS))
    p.add_argument("--release", required=True)
    p.set_defaults(func=cmd_start)
    p = sub.add_parser("restart", help="recreate a colour's container from its release tag and config")
    p.add_argument("color", choices=list(PORTS))
    p.set_defaults(func=cmd_restart)
    p = sub.add_parser("stop")
    p.add_argument("color", choices=list(PORTS))
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_stop)
    p = sub.add_parser("lb", help="start or stop the nginx load balancer container")
    p.add_argument("action", choices=["start", "stop"])
    p.set_defaults(func=cmd_lb)
    sub.add_parser("baseline").set_defaults(func=cmd_baseline)
    p = sub.add_parser("precheck")
    p.add_argument("color", choices=list(PORTS))
    p.set_defaults(func=cmd_precheck)
    p = sub.add_parser("cutover")
    p.add_argument("color", choices=list(PORTS))
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_cutover)
    p = sub.add_parser("rollback")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_rollback)
    sub.add_parser("seed", help="copy the approved warehouse into the shared volume again").set_defaults(func=cmd_seed)
    p = sub.add_parser("note")
    p.add_argument("text", nargs="+")
    p.set_defaults(func=cmd_note)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("timeline").set_defaults(func=cmd_timeline)
    p = sub.add_parser("logs")
    p.add_argument("service", choices=[*PORTS, "lb"])
    p.add_argument("--tail", type=int, default=40)
    p.set_defaults(func=cmd_logs)
    p = sub.add_parser("diff", help="show source changes between two release images")
    p.add_argument("old")
    p.add_argument("new")
    p.set_defaults(func=cmd_diff)
    sub.add_parser("down", help="remove containers and volumes").set_defaults(func=cmd_down)
    args = parser.parse_args()
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        if "docker" in str(exc):
            print("docker not found: install Docker Desktop and make sure it is running")
            return 2
        raise
    except DeployError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
