from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from deploy import bluegreen as bg  # noqa: E402

STALE_DB = PROJECT / "data" / "warehouse" / "settlement_db_v2.duckdb"
STALE_CUTOFF = "2026-09-05 00:00:00"
BUILD_CONTEXT = ["Dockerfile", "requirements.txt", "src", "sql", "frontend"]

DEFECT_A_SUMMARY = '''def settlement_summary(start: date, end: date, merchant_id: str | None) -> dict:
    sql = """
        SELECT count(DISTINCT t.transaction_id)                                          AS transaction_count,
               coalesce(sum(CASE WHEN s.settlement_status = 'SETTLED' THEN t.amount END), 0) AS settled_amount
        FROM gold.fact_transaction t
        JOIN gold.dim_merchant m ON m.merchant_key = t.merchant_key
        LEFT JOIN gold.fact_settlement s ON s.transaction_id = t.transaction_id
        WHERE t.is_successful
          AND t.transaction_date BETWEEN ? AND ?
          AND (? IS NULL OR m.merchant_id = ?)
    """
    row = _fetch(sql, [start, end, merchant_id, merchant_id])[0]
    agg = _fetch(
        """
        SELECT coalesce(sum(transaction_amount), 0) AS transaction_amount,
               coalesce(sum(unsettled_count), 0)    AS unsettled_count,
               coalesce(sum(sla_eligible_count), 0) AS sla_eligible_count,
               coalesce(sum(sla_met_count), 0)      AS sla_met_count
        FROM gold.agg_daily_merchant_settlement
        WHERE transaction_date BETWEEN ? AND ?
          AND (? IS NULL OR merchant_id = ?)
        """,
        [start, end, merchant_id, merchant_id],
    )[0]
    txn_amount = float(agg["transaction_amount"])
    settled = float(row["settled_amount"])
    eligible = int(agg["sla_eligible_count"])
    return {
        "transaction_count": int(row["transaction_count"]),
        "transaction_amount": txn_amount,
        "settled_amount": settled,
        "settlement_gap": txn_amount - settled,
        "unsettled_count": int(agg["unsettled_count"]),
        "settlement_rate": round(100.0 * settled / txn_amount, 2) if txn_amount else 0.0,
        "sla_rate": round(100.0 * int(agg["sla_met_count"]) / eligible, 2) if eligible else 0.0,
    }


'''

DEFECT_A_TREND = '''def daily_trend(start: date, end: date, merchant_id: str | None) -> list[dict]:
    sql = """
        WITH joined AS (
            SELECT t.transaction_date, t.transaction_id, t.amount, s.settlement_status
            FROM gold.fact_transaction t
            JOIN gold.dim_merchant m ON m.merchant_key = t.merchant_key
            LEFT JOIN gold.fact_settlement s ON s.transaction_id = t.transaction_id
            WHERE t.is_successful
              AND t.transaction_date BETWEEN ? AND ?
              AND (? IS NULL OR m.merchant_id = ?)
        )
        SELECT a.transaction_date,
               a.transaction_amount,
               j.settled_amount,
               a.transaction_amount - j.settled_amount AS settlement_gap,
               CASE WHEN a.transaction_amount = 0 THEN 0
                    ELSE round(100.0 * j.settled_amount / a.transaction_amount, 2) END AS settlement_rate,
               a.sla_rate
        FROM (
            SELECT transaction_date, sum(transaction_amount) AS transaction_amount,
                   CASE WHEN sum(sla_eligible_count) = 0 THEN 0
                        ELSE round(100.0 * sum(sla_met_count) / sum(sla_eligible_count), 2) END AS sla_rate
            FROM gold.agg_daily_merchant_settlement
            WHERE transaction_date BETWEEN ? AND ? AND (? IS NULL OR merchant_id = ?)
            GROUP BY transaction_date
        ) a
        JOIN (
            SELECT transaction_date,
                   coalesce(sum(CASE WHEN settlement_status = 'SETTLED' THEN amount END), 0) AS settled_amount
            FROM joined GROUP BY transaction_date
        ) j USING (transaction_date)
        ORDER BY transaction_date
    """
    rows = _fetch(sql, [start, end, merchant_id, merchant_id, start, end, merchant_id, merchant_id])
    for row in rows:
        for key in ("transaction_amount", "settled_amount", "settlement_gap"):
            row[key] = float(row[key])
    return rows


'''


def replace_function(source: str, name: str, new_body: str) -> str:
    match = re.search(rf"^def {name}\(.*?(?=^def |\Z)", source, flags=re.S | re.M)
    if not match:
        raise SystemExit(f"function {name} not found")
    return source[: match.start()] + new_body + source[match.end():]


def function_block(source: str, name: str) -> tuple[int, int]:
    match = re.search(rf"^def {name}\(.*?(?=^def |\Z)", source, flags=re.S | re.M)
    return match.start(), match.end()


def defect_a(repo: Path) -> str:
    src = repo.read_text(encoding="utf-8")
    src = replace_function(src, "settlement_summary", DEFECT_A_SUMMARY)
    src = replace_function(src, "daily_trend", DEFECT_A_TREND)
    repo.write_text(src, encoding="utf-8")
    return "settlement queries now join fact_transaction to fact_settlement before aggregating"


def defect_b(repo: Path) -> str:
    src = repo.read_text(encoding="utf-8")
    for name in ("settlement_summary", "daily_trend"):
        start, end = function_block(src, name)
        block = re.sub(r"\b(\w+\.)?transaction_date BETWEEN \? AND \?", r"CAST(\1_loaded_at AS DATE) BETWEEN ? AND ?", src[start:end])
        src = src[:start] + block + src[end:]
    repo.write_text(src, encoding="utf-8")
    return "date window now filters on load (ingestion) time instead of transaction time"


def build_stale_warehouse() -> None:
    raw = PROJECT / "data" / "raw"
    with tempfile.TemporaryDirectory() as tmp:
        stale = Path(tmp)
        for f in raw.glob("*.csv"):
            if f.name.startswith("settlements"):
                with open(f, newline="", encoding="utf-8") as src, open(stale / f.name, "w", newline="", encoding="utf-8") as dst:
                    reader, writer = csv.reader(src), csv.writer(dst)
                    header = next(reader)
                    writer.writerow(header)
                    writer.writerows(r for r in reader if r[2] < STALE_CUTOFF)
            else:
                shutil.copy2(f, stale / f.name)
        STALE_DB.unlink(missing_ok=True)
        from src.pipeline import run_pipeline

        run_pipeline.run(raw_dir=str(stale), warehouse=str(STALE_DB))


def defect_c(color: str) -> str:
    state = bg.load_state()
    if not bg.container_image_id(state, color):
        raise SystemExit(f"{color} is not running")
    build_stale_warehouse()
    target = f"{bg.CONTAINER_WAREHOUSE_DIR}/{STALE_DB.name}"
    bg.compose(state, "cp", str(STALE_DB), f"api-{color}:{target}")
    bg.env_file(color).write_text(f"WAREHOUSE_PATH={target}\n", encoding="utf-8")
    return f"{color} configuration now points at {STALE_DB.name} (a stale snapshot)"


def build_tampered_image(release: str, patch_names: list[str]) -> None:
    state = bg.load_state()
    info = state["releases"].get(release)
    if not info:
        raise SystemExit(f"release {release} not found")
    with tempfile.TemporaryDirectory() as tmp:
        ctx = Path(tmp)
        for item in BUILD_CONTEXT:
            src = PROJECT / item
            if src.is_dir():
                shutil.copytree(src, ctx / item, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(src, ctx / item)
        repo = ctx / "src" / "api" / "repository.py"
        for name in patch_names:
            {"A": defect_a, "B": defect_b, "D": defect_d}[name](repo)
        cmd = ["docker", "build", "-q", "-t", info["image"], "--build-arg", f"APP_VERSION={info['version']}"]
        if os.getenv("PYTHON_IMAGE"):
            cmd += ["--build-arg", f"PYTHON_IMAGE={os.environ['PYTHON_IMAGE']}"]
        proc = subprocess.run([*cmd, str(ctx)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"docker build failed:\n{proc.stderr[-1500:]}")


def defect_d(repo: Path) -> str:
    src = repo.read_text(encoding="utf-8")
    start, end = function_block(src, "settlement_summary")
    block = src[start:end]
    new_block = re.sub(r'"settlement_rate": round\(100\.0 \* settled / txn_amount, 2\)',
                       '"settlement_rate": round(100.0 * settled / int(row["transaction_count"]), 2)', block)
    if new_block == block:
        raise SystemExit("settlement_rate expression not found")
    repo.write_text(src[:start] + new_block + src[end:], encoding="utf-8")
    return "settlement_rate now divides settled amount by transaction count"


def main() -> int:
    parser = argparse.ArgumentParser(description="Inject a production defect into a deployed release (incident drill)")
    parser.add_argument("--release", default="v2", help="release tag whose image is replaced (defects A, B, D)")
    parser.add_argument("--color", default="green", help="colour whose configuration is changed (defect C)")
    parser.add_argument("--defect", action="append", choices=["A", "B", "C", "D"], required=True)
    args = parser.parse_args()

    names = {"A": "A grain", "B": "B date", "C": "C configuration", "D": "D calculation"}
    what = {"A": "settlement queries join fact_transaction to fact_settlement before aggregating",
            "B": "date window filters on load (ingestion) time instead of transaction time",
            "D": "settlement_rate divides settled amount by transaction count"}
    applied = []
    code_defects = [d for d in args.defect if d != "C"]
    if code_defects:
        build_tampered_image(args.release, code_defects)
        applied += [(names[d], f"image {bg.IMAGE_REPO}:{args.release} rebuilt: {what[d]}") for d in code_defects]
    if "C" in args.defect:
        applied.append((names["C"], defect_c(args.color)))

    log = PROJECT / "deploy" / "logs" / "facilitator_injections.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        for name, detail in applied:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} defect={name}: {detail}\n")
    print(f"Injected {len(applied)} defect(s). Details are in {log.relative_to(PROJECT).as_posix()} (facilitator only).")
    print(f"Apply to production with: python -m deploy.bluegreen restart {args.color}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
