from __future__ import annotations

import argparse
import json
import sys

from src import db


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail the pipeline when data quality breaches thresholds")
    parser.add_argument("--max-critical", type=int, default=0)
    parser.add_argument("--max-quarantine-pct", type=float, default=2.0)
    parser.add_argument("--warehouse", default=None)
    parser.add_argument("--report", default="dq_report.json")
    args = parser.parse_args()

    with db.session(args.warehouse, read_only=True) as con:
        rules = db.query_dicts(
            con,
            """
            SELECT rule_id, source_table, severity, disposition, sum(failed_rows) AS failed_rows
            FROM ctl.dq_rule_result
            GROUP BY rule_id, source_table, severity, disposition
            HAVING sum(failed_rows) > 0
            ORDER BY failed_rows DESC
            """,
        )
        source_rows = db.scalar(con, "SELECT count(*) FROM bronze.transactions") or 0
        quarantined = db.scalar(
            con,
            "SELECT count(*) FROM ctl.dq_quarantine WHERE disposition IN ('REJECT','QUARANTINE')",
        ) or 0
        last_run = db.query_dicts(
            con, "SELECT batch_id, status, silver_rows, quarantined_rows FROM ctl.pipeline_run ORDER BY started_at DESC LIMIT 1"
        )

    critical = sum(int(r["failed_rows"]) for r in rules if r["severity"] == "CRITICAL")
    pct = (100.0 * quarantined / source_rows) if source_rows else 0.0
    report = {
        "critical_failures": critical,
        "quarantined_records": int(quarantined),
        "source_rows": int(source_rows),
        "quarantine_pct": round(pct, 3),
        "rules": [{**r, "failed_rows": int(r["failed_rows"])} for r in rules],
        "last_run": last_run[0] if last_run else None,
    }
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)

    print(json.dumps(report, indent=2, default=str))
    if critical > args.max_critical:
        print(f"FAIL: {critical} critical data quality failures (max {args.max_critical})")
        return 1
    if pct > args.max_quarantine_pct:
        print(f"FAIL: {pct:.2f}% of source records quarantined (max {args.max_quarantine_pct}%)")
        return 1
    print("PASS: data quality within thresholds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
