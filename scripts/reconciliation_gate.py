from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import duckdb

from scripts.http_safety import safe_get
from src.config import settings, utcnow

AMOUNT_TOLERANCE = 1.0
RATE_TOLERANCE = 0.011
DRIFT_RATE_PP = 0.5
DRIFT_AMOUNT_PCT = 0.5

INDEPENDENT_KPI_SQL = """
    WITH txn AS (
        SELECT t.transaction_id, t.transaction_ts, t.amount, m.merchant_id
        FROM gold.fact_transaction t
        JOIN gold.dim_merchant m ON m.merchant_key = t.merchant_key
        WHERE t.is_successful
          AND t.transaction_date BETWEEN ? AND ?
    ),
    stl AS (
        SELECT s.transaction_id,
               sum(s.settlement_amount) FILTER (WHERE s.settlement_status = 'SETTLED') AS settled,
               min(s.settlement_ts)     FILTER (WHERE s.settlement_status = 'SETTLED') AS first_settled
        FROM gold.fact_settlement s
        WHERE NOT s.is_orphan
          AND s.transaction_id IN (SELECT transaction_id FROM txn)
        GROUP BY s.transaction_id
    )
    SELECT txn.merchant_id,
           count(*)                                   AS transaction_count,
           sum(txn.amount)                            AS transaction_amount,
           coalesce(sum(stl.settled), 0)              AS settled_amount,
           count(stl.first_settled)                   AS sla_eligible,
           count(*) FILTER (WHERE date_diff('second', txn.transaction_ts, stl.first_settled) <= ?) AS sla_met
    FROM txn
    LEFT JOIN stl ON stl.transaction_id = txn.transaction_id
    GROUP BY txn.merchant_id
"""


def _kpis(transaction_count: int, transaction_amount: float, settled_amount: float, eligible: int, met: int) -> dict:
    return {
        "transaction_count": int(transaction_count),
        "transaction_amount": round(float(transaction_amount), 2),
        "settled_amount": round(float(settled_amount), 2),
        "settlement_gap": round(float(transaction_amount) - float(settled_amount), 2),
        "settlement_rate": round(100.0 * float(settled_amount) / float(transaction_amount), 2) if transaction_amount else 0.0,
        "sla_rate": round(100.0 * met / eligible, 2) if eligible else 0.0,
    }


def expected_kpis(warehouse: str, start: str, end: str) -> dict:
    con = duckdb.connect(warehouse, read_only=True)
    try:
        rows = con.execute(INDEPENDENT_KPI_SQL, [start, end, settings.settlement_sla_minutes * 60]).fetchall()
        agg = con.execute(
            """
            SELECT coalesce(sum(success_count), 0), coalesce(sum(transaction_amount), 0), coalesce(sum(settled_amount), 0),
                   coalesce(sum(sla_eligible_count), 0), coalesce(sum(sla_met_count), 0)
            FROM gold.agg_daily_merchant_settlement
            WHERE transaction_date BETWEEN ? AND ?
            """,
            [start, end],
        ).fetchone()
        over_settled = {
            r[0]
            for r in con.execute(
                """
                SELECT DISTINCT m.merchant_id
                FROM ctl.dq_quarantine q
                JOIN gold.fact_transaction t ON t.transaction_id = q.business_key
                JOIN gold.dim_merchant m ON m.merchant_key = t.merchant_key
                WHERE q.rule_id = 'STL-008'
                """
            ).fetchall()
        }
    finally:
        con.close()

    merchants = {r[0]: _kpis(r[1], r[2], r[3], r[4], r[5]) for r in rows}
    totals = [sum(r[i] for r in rows) for i in range(1, 6)]
    return {
        "window": {"start_date": start, "end_date": end},
        "platform": _kpis(*totals) if rows else _kpis(0, 0, 0, 0, 0),
        "aggregate_table": _kpis(*agg),
        "merchants": merchants,
        "over_settlement_exceptions": sorted(over_settled),
    }


def expected_exceptions(expected: dict, rate_threshold: float, sla_threshold: float) -> set[str]:
    return {
        merchant_id
        for merchant_id, k in expected["merchants"].items()
        if merchant_id != "UNKNOWN"
        and k["transaction_amount"] > 0
        and (k["settlement_rate"] < rate_threshold or k["sla_rate"] < sla_threshold)
    }


def _check(name: str, passed: bool, detail: str, warn: bool = False) -> dict:
    status = "PASS" if passed else ("WARN" if warn else "FAIL")
    return {"check": name, "status": status, "detail": detail}


def compare_summary(observed: dict, expected: dict, label: str) -> list[dict]:
    checks = []
    rate = observed.get("settlement_rate")
    sla = observed.get("sla_rate")
    checks.append(
        _check(f"{label}_rate_within_bounds", rate is not None and 0 <= rate <= 100 and sla is not None and 0 <= sla <= 100,
               f"settlement_rate={rate} sla_rate={sla}")
    )
    checks.append(
        _check(f"{label}_settled_not_above_transacted",
               observed.get("settled_amount", 0) <= observed.get("transaction_amount", 0) + AMOUNT_TOLERANCE,
               f"settled={observed.get('settled_amount')} transacted={observed.get('transaction_amount')}")
    )
    mismatches = []
    for key in ("transaction_count", "transaction_amount", "settled_amount", "settlement_gap"):
        if abs(float(observed.get(key, 0)) - float(expected[key])) > (0 if key == "transaction_count" else AMOUNT_TOLERANCE):
            mismatches.append(f"{key}: observed={observed.get(key)} expected={expected[key]}")
    for key in ("settlement_rate", "sla_rate"):
        if abs(float(observed.get(key, 0)) - float(expected[key])) > RATE_TOLERANCE:
            mismatches.append(f"{key}: observed={observed.get(key)} expected={expected[key]}")
    checks.append(
        _check(f"{label}_matches_independent_recompute", not mismatches, "; ".join(mismatches) or "all KPIs match")
    )
    return checks


def check_warehouse(expected: dict) -> list[dict]:
    checks = compare_summary(expected["aggregate_table"], expected["platform"], "aggregate_table")
    over = []
    excused = set(expected["over_settlement_exceptions"])
    for merchant_id, k in expected["merchants"].items():
        if k["settled_amount"] > k["transaction_amount"] + AMOUNT_TOLERANCE:
            over.append(merchant_id)
    unexplained = [m for m in over if m not in excused]
    checks.append(
        _check("facts_merchant_settled_not_above_transacted", not unexplained,
               f"unexplained={unexplained} excused_by_STL_008={sorted(set(over) & excused)}",
               warn=bool(over) and not unexplained)
    )
    return checks


def check_api(client: Any, expected: dict, rate_threshold: float, sla_threshold: float) -> list[dict]:
    window = expected["window"]
    checks = []
    response = safe_get(client, "/api/v1/settlement-summary", window)
    if response.status_code != 200:
        return [_check("api_settlement_summary_http", False, f"HTTP {response.status_code}: {response.text[:200]}")]
    checks.extend(compare_summary(response.json(), expected["platform"], "api_platform"))

    over, errors, mismatched = [], [], []
    for merchant_id, k in sorted(expected["merchants"].items()):
        if merchant_id == "UNKNOWN":
            continue
        r = safe_get(client, "/api/v1/settlement-summary", {**window, "merchant_id": merchant_id})
        if r.status_code != 200:
            errors.append(f"{merchant_id}:HTTP{r.status_code}")
            continue
        body = r.json()
        if body["settled_amount"] > body["transaction_amount"] + AMOUNT_TOLERANCE:
            over.append(f"{merchant_id} settled={body['settled_amount']:.2f} > transacted={body['transaction_amount']:.2f} "
                        f"({body['settlement_rate']}%)")
        if abs(body["settled_amount"] - k["settled_amount"]) > AMOUNT_TOLERANCE:
            mismatched.append(merchant_id)
    checks.append(_check("api_merchant_http", not errors, f"errors={errors}"))
    checks.append(_check("api_merchant_settled_not_above_transacted", not over,
                         f"{len(over)} merchants over 100%: " + "; ".join(over[:10]) if over else "no merchant above 100%"))
    checks.append(_check("api_merchant_settled_matches_facts", not mismatched,
                         f"{len(mismatched)} merchants disagree with facts: {mismatched[:10]}" if mismatched else "all merchants match"))

    r = safe_get(client, "/api/v1/merchant-exceptions",
                 {**window, "settlement_rate_threshold": rate_threshold,
                  "sla_rate_threshold": sla_threshold, "limit": 500})
    if r.status_code != 200:
        checks.append(_check("api_exceptions_http", False, f"HTTP {r.status_code}"))
    else:
        observed = {row["merchant_id"] for row in r.json()}
        wanted = expected_exceptions(expected, rate_threshold, sla_threshold)
        checks.append(_check("api_exceptions_match_facts", observed == wanted,
                             f"missing={sorted(wanted - observed)} unexpected={sorted(observed - wanted)}"))
    return checks


def check_baseline(current: dict, baseline: dict) -> list[dict]:
    b, c = baseline["platform"], current
    notes = []
    if abs(c["settlement_rate"] - b["settlement_rate"]) > DRIFT_RATE_PP:
        notes.append(f"settlement_rate {b['settlement_rate']} -> {c['settlement_rate']}")
    if abs(c["sla_rate"] - b["sla_rate"]) > DRIFT_RATE_PP:
        notes.append(f"sla_rate {b['sla_rate']} -> {c['sla_rate']}")
    for key in ("transaction_amount", "settled_amount"):
        base = b[key] or 1.0
        if abs(c[key] - b[key]) / base * 100 > DRIFT_AMOUNT_PCT:
            notes.append(f"{key} {b[key]:.2f} -> {c[key]:.2f}")
    if c["transaction_count"] != b["transaction_count"]:
        notes.append(f"transaction_count {b['transaction_count']} -> {c['transaction_count']}")
    return [_check("baseline_drift", not notes, "; ".join(notes) or "within drift thresholds of baseline")]


def run(warehouse: str, start: str, end: str, client: Any = None, baseline: dict | None = None,
        rate_threshold: float | None = None, sla_threshold: float | None = None) -> dict:
    rate_threshold = settings.settlement_rate_threshold if rate_threshold is None else rate_threshold
    sla_threshold = settings.sla_rate_threshold if sla_threshold is None else sla_threshold
    expected = expected_kpis(warehouse, start, end)
    checks = check_warehouse(expected)
    observed = expected["aggregate_table"]
    if client is not None:
        api_checks = check_api(client, expected, rate_threshold, sla_threshold)
        checks.extend(api_checks)
        r = safe_get(client, "/api/v1/settlement-summary", expected["window"])
        if r.status_code == 200:
            observed = {k: r.json()[k] for k in expected["platform"]}
    if baseline is not None:
        checks.extend(check_baseline(observed, baseline))
    return {
        "generated_at": utcnow().isoformat(),
        "warehouse": str(warehouse),
        "window": expected["window"],
        "expected_from_facts": expected["platform"],
        "observed": observed,
        "checks": checks,
        "passed": all(c["status"] != "FAIL" for c in checks),
    }


def print_report(report: dict) -> None:
    print(f"KPI reconciliation  window={report['window']['start_date']}..{report['window']['end_date']}")
    e, o = report["expected_from_facts"], report["observed"]
    print(f"  expected (facts): rate={e['settlement_rate']}% sla={e['sla_rate']}% settled={e['settled_amount']:,.2f} of {e['transaction_amount']:,.2f}")
    print(f"  observed        : rate={o['settlement_rate']}% sla={o['sla_rate']}% settled={o['settled_amount']:,.2f} of {o['transaction_amount']:,.2f}")
    for c in report["checks"]:
        print(f"  [{c['status']}] {c['check']}: {c['detail']}")
    print("RESULT:", "PASS" if report["passed"] else "FAIL")


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile settlement KPIs against an independent recompute from the fact tables")
    parser.add_argument("--warehouse", default=settings.warehouse_path, help="approved production warehouse (source of truth)")
    parser.add_argument("--start", default="2026-09-01")
    parser.add_argument("--end", default="2026-09-07")
    parser.add_argument("--api-url", default=None, help="also verify what the running API returns")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--baseline", default=None, help="baseline KPI file captured from the previous release")
    parser.add_argument("--write-baseline", default=None, help="save the expected KPIs as a baseline file")
    parser.add_argument("--out", default=None, help="write the JSON report here")
    args = parser.parse_args()

    if not Path(args.warehouse).exists():
        print(f"warehouse not found: {args.warehouse}")
        return 2

    if args.write_baseline:
        expected = expected_kpis(args.warehouse, args.start, args.end)
        Path(args.write_baseline).parent.mkdir(parents=True, exist_ok=True)
        Path(args.write_baseline).write_text(json.dumps(expected, indent=2), encoding="utf-8")
        print(f"baseline written to {args.write_baseline}: rate={expected['platform']['settlement_rate']}%")
        return 0

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8")) if args.baseline else None
    client = None
    if args.api_url:
        import httpx

        client = httpx.Client(base_url=args.api_url, timeout=15,
                              headers={"X-API-Key": args.api_key or settings.api_key})
    report = run(args.warehouse, args.start, args.end, client=client, baseline=baseline)
    print_report(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
