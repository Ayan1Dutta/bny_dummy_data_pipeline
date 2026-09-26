from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from scripts.http_safety import safe_get
from src.config import settings, utcnow

REQUIRED_SUMMARY_FIELDS = ("transaction_count", "transaction_amount", "settled_amount",
                           "settlement_rate", "settlement_gap", "sla_rate")
REQUIRED_EXCEPTION_FIELDS = ("merchant_id", "merchant_name", "settlement_rate", "sla_rate", "risk_level")
AMOUNT_TOLERANCE = 1.0
RATE_TOLERANCE = 0.011


def _timed_get(client: Any, path: str, params: dict | None = None) -> tuple[Any, float]:
    started = time.perf_counter()
    response = safe_get(client, path, params)
    return response, round((time.perf_counter() - started) * 1000, 1)


def _json(response: Any) -> tuple[Any, str | None]:
    try:
        return response.json(), None
    except ValueError as exc:
        return None, f"invalid JSON: {exc}"


def _result(name: str, passed: bool, detail: str, latency_ms: float | None = None) -> dict:
    return {"check": name, "status": "PASS" if passed else "FAIL", "latency_ms": latency_ms, "detail": detail}


def run_smoke(client: Any, start: str, end: str, expected: dict | None = None, latency_budget_ms: float = 500.0) -> dict:
    window = {"start_date": start, "end_date": end}
    results = []

    r, ms = _timed_get(client, "/health")
    body, err = _json(r)
    results.append(_result("health", r.status_code == 200 and not err and body.get("status") == "ok",
                           f"HTTP {r.status_code} version={body.get('version') if body else None}", ms))
    version = body.get("version") if body else None

    r, ms = _timed_get(client, "/ready")
    body, err = _json(r)
    ready_ok = r.status_code == 200 and not err and body.get("warehouse_reachable") is True
    results.append(_result("ready_database_connectivity", ready_ok,
                           f"HTTP {r.status_code} gold_rows={body.get('gold_rows') if body else None}", ms))
    results.append(_result("pipeline_last_run_success", ready_ok and body.get("last_run_status") == "SUCCESS",
                           f"last_batch={body.get('last_batch_id') if body else None} "
                           f"status={body.get('last_run_status') if body else None}"))

    r, ms = _timed_get(client, "/api/v1/settlement-summary", window)
    summary, err = _json(r)
    ok = r.status_code == 200 and not err and all(f in summary for f in REQUIRED_SUMMARY_FIELDS)
    results.append(_result("settlement_summary_http_json", ok, f"HTTP {r.status_code} {err or ''}".strip(), ms))
    results.append(_result("settlement_summary_latency", ms < latency_budget_ms, f"{ms} ms (budget {latency_budget_ms} ms)", ms))
    if ok:
        in_bounds = 0 <= summary["settlement_rate"] <= 100 and 0 <= summary["sla_rate"] <= 100
        results.append(_result("settlement_rate_within_0_100", in_bounds,
                               f"settlement_rate={summary['settlement_rate']} sla_rate={summary['sla_rate']}"))
        results.append(_result("settled_not_above_transacted",
                               summary["settled_amount"] <= summary["transaction_amount"] + AMOUNT_TOLERANCE,
                               f"settled={summary['settled_amount']:.2f} transacted={summary['transaction_amount']:.2f}"))
        if expected:
            diffs = []
            for key in REQUIRED_SUMMARY_FIELDS:
                tolerance = RATE_TOLERANCE if key.endswith("rate") else (0 if key == "transaction_count" else AMOUNT_TOLERANCE)
                if abs(float(summary[key]) - float(expected[key])) > tolerance:
                    diffs.append(f"{key}: got {summary[key]} expected {expected[key]}")
            results.append(_result("kpi_values_match_expected", not diffs, "; ".join(diffs) or "all six KPIs match"))

    r, ms = _timed_get(client, "/api/v1/merchant-exceptions", window)
    rows, err = _json(r)
    ok = r.status_code == 200 and not err and isinstance(rows, list)
    results.append(_result("merchant_exceptions_http_json", ok, f"HTTP {r.status_code} rows={len(rows) if ok else None}", ms))
    results.append(_result("merchant_exceptions_latency", ms < latency_budget_ms, f"{ms} ms (budget {latency_budget_ms} ms)", ms))
    if ok:
        bad = [row.get("merchant_id") for row in rows
               if not all(f in row for f in REQUIRED_EXCEPTION_FIELDS)
               or not (0 <= row["settlement_rate"] <= 100 and 0 <= row["sla_rate"] <= 100)]
        results.append(_result("merchant_exceptions_contract", not bad, f"invalid rows={bad}" if bad else f"{len(rows)} rows valid"))

    return {
        "generated_at": utcnow().isoformat(),
        "version": version,
        "window": window,
        "summary": summary if isinstance(summary, dict) else None,
        "results": results,
        "passed": all(x["status"] == "PASS" for x in results),
    }


def print_report(report: dict, target: str) -> None:
    print(f"Smoke test  target={target}  version={report['version']}")
    for x in report["results"]:
        latency = f" [{x['latency_ms']} ms]" if x["latency_ms"] is not None else ""
        print(f"  [{x['status']}] {x['check']}{latency}: {x['detail']}")
    print("RESULT:", "PASS" if report["passed"] else "FAIL")


def main() -> int:
    parser = argparse.ArgumentParser(description="Production smoke test for the settlement API")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--start", default="2026-09-01")
    parser.add_argument("--end", default="2026-09-07")
    parser.add_argument("--expected", default=None, help="baseline file from reconciliation_gate --write-baseline")
    parser.add_argument("--latency-budget-ms", type=float, default=500.0)
    parser.add_argument("--out", default=None)
    parser.add_argument("--save-as-baseline", default=None, help="after a passing run, save the observed KPIs as the baseline")
    args = parser.parse_args()

    import httpx

    expected = None
    if args.expected:
        expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))["platform"]
    with httpx.Client(base_url=args.base_url, timeout=15,
                      headers={"X-API-Key": args.api_key or settings.api_key}) as client:
        try:
            report = run_smoke(client, args.start, args.end, expected, args.latency_budget_ms)
        except httpx.HTTPError as exc:
            print(f"Smoke test could not reach {args.base_url}: {exc}")
            return 2
    print_report(report, args.base_url)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if args.save_as_baseline and report["passed"] and report["summary"]:
        baseline = {"platform": {k: report["summary"][k] for k in REQUIRED_SUMMARY_FIELDS},
                    "window": report["window"], "captured_from": f"{args.base_url} version={report['version']}"}
        Path(args.save_as_baseline).parent.mkdir(parents=True, exist_ok=True)
        Path(args.save_as_baseline).write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
