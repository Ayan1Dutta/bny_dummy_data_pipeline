from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

import duckdb

from src.config import settings

_lock = threading.Lock()
_con: duckdb.DuckDBPyConnection | None = None
_warehouse_override: str | None = None


def warehouse_path() -> str:
    return _warehouse_override or settings.warehouse_path


def set_warehouse(path: str) -> None:
    global _warehouse_override
    reset_connection()
    _warehouse_override = path


def get_connection() -> duckdb.DuckDBPyConnection:
    global _con
    with _lock:
        if _con is None:
            target = warehouse_path()
            if not Path(target).exists():
                raise FileNotFoundError(f"warehouse not found at {target}")
            _con = duckdb.connect(target, read_only=True)
        return _con


def reset_connection() -> None:
    global _con
    with _lock:
        if _con is not None:
            _con.close()
        _con = None


def _fetch(sql: str, params: list) -> list[dict]:
    con = get_connection()
    with _lock:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def merchant_exists(merchant_id: str) -> bool:
    rows = _fetch("SELECT 1 FROM gold.dim_merchant WHERE merchant_id = ? LIMIT 1", [merchant_id])
    return bool(rows)


def settlement_summary(start: date, end: date, merchant_id: str | None) -> dict:
    sql = """
        SELECT coalesce(sum(success_count), 0)                AS transaction_count,
               coalesce(sum(transaction_amount), 0)           AS transaction_amount,
               coalesce(sum(settled_amount), 0)               AS settled_amount,
               coalesce(sum(settlement_gap), 0)               AS settlement_gap,
               coalesce(sum(unsettled_count), 0)              AS unsettled_count,
               coalesce(sum(sla_eligible_count), 0)           AS sla_eligible_count,
               coalesce(sum(sla_met_count), 0)                AS sla_met_count
        FROM gold.agg_daily_merchant_settlement
        WHERE transaction_date BETWEEN ? AND ?
          AND (? IS NULL OR merchant_id = ?)
    """
    row = _fetch(sql, [start, end, merchant_id, merchant_id])[0]
    txn_amount = float(row["transaction_amount"])
    settled = float(row["settled_amount"])
    eligible = int(row["sla_eligible_count"])
    return {
        "transaction_count": int(row["transaction_count"]),
        "transaction_amount": txn_amount,
        "settled_amount": settled,
        "settlement_gap": float(row["settlement_gap"]),
        "unsettled_count": int(row["unsettled_count"]),
        "settlement_rate": round(100.0 * settled / txn_amount, 2) if txn_amount else 0.0,
        "sla_rate": round(100.0 * int(row["sla_met_count"]) / eligible, 2) if eligible else 0.0,
    }


def merchant_exceptions(start: date, end: date, rate_threshold: float, sla_threshold: float, limit: int) -> list[dict]:
    sql = """
        WITH m AS (
            SELECT merchant_id,
                   arg_max(merchant_name, transaction_date) AS merchant_name,
                   arg_max(risk_level, transaction_date)    AS risk_level,
                   sum(transaction_amount)                  AS transaction_amount,
                   sum(settled_amount)                      AS settled_amount,
                   sum(settlement_gap)                      AS settlement_gap,
                   sum(unsettled_count)                     AS unsettled_count,
                   sum(sla_eligible_count)                  AS sla_eligible_count,
                   sum(sla_met_count)                       AS sla_met_count
            FROM gold.agg_daily_merchant_settlement
            WHERE transaction_date BETWEEN ? AND ?
              AND merchant_key <> -1
            GROUP BY merchant_id
        )
        SELECT merchant_id, merchant_name, risk_level, transaction_amount, settlement_gap, unsettled_count,
               CASE WHEN transaction_amount = 0 THEN 0
                    ELSE round(100.0 * settled_amount / transaction_amount, 2) END AS settlement_rate,
               CASE WHEN sla_eligible_count = 0 THEN 0
                    ELSE round(100.0 * sla_met_count / sla_eligible_count, 2) END AS sla_rate
        FROM m
        WHERE (transaction_amount > 0)
          AND ((CASE WHEN transaction_amount = 0 THEN 0 ELSE 100.0 * settled_amount / transaction_amount END) < ?
            OR (CASE WHEN sla_eligible_count = 0 THEN 0 ELSE 100.0 * sla_met_count / sla_eligible_count END) < ?)
        ORDER BY settlement_gap DESC
        LIMIT ?
    """
    rows = _fetch(sql, [start, end, rate_threshold, sla_threshold, limit])
    for row in rows:
        reasons = []
        if row["settlement_rate"] < rate_threshold:
            reasons.append(f"settlement_rate<{rate_threshold}")
        if row["sla_rate"] < sla_threshold:
            reasons.append(f"sla_rate<{sla_threshold}")
        row["breach_reasons"] = reasons
        row["transaction_amount"] = float(row["transaction_amount"])
        row["settlement_gap"] = float(row["settlement_gap"])
        row["unsettled_count"] = int(row["unsettled_count"])
    return rows


def daily_trend(start: date, end: date, merchant_id: str | None) -> list[dict]:
    sql = """
        SELECT transaction_date,
               sum(transaction_amount) AS transaction_amount,
               sum(settled_amount)     AS settled_amount,
               sum(settlement_gap)     AS settlement_gap,
               CASE WHEN sum(transaction_amount) = 0 THEN 0
                    ELSE round(100.0 * sum(settled_amount) / sum(transaction_amount), 2) END AS settlement_rate,
               CASE WHEN sum(sla_eligible_count) = 0 THEN 0
                    ELSE round(100.0 * sum(sla_met_count) / sum(sla_eligible_count), 2) END AS sla_rate
        FROM gold.agg_daily_merchant_settlement
        WHERE transaction_date BETWEEN ? AND ?
          AND (? IS NULL OR merchant_id = ?)
        GROUP BY transaction_date
        ORDER BY transaction_date
    """
    rows = _fetch(sql, [start, end, merchant_id, merchant_id])
    for row in rows:
        for key in ("transaction_amount", "settled_amount", "settlement_gap"):
            row[key] = float(row[key])
    return rows


def top_merchant_gaps(start: date, end: date, limit: int) -> list[dict]:
    sql = """
        SELECT merchant_id,
               arg_max(merchant_name, transaction_date) AS merchant_name,
               arg_max(risk_level, transaction_date)    AS risk_level,
               sum(settlement_gap)                      AS settlement_gap,
               CASE WHEN sum(transaction_amount) = 0 THEN 0
                    ELSE round(100.0 * sum(settled_amount) / sum(transaction_amount), 2) END AS settlement_rate
        FROM gold.agg_daily_merchant_settlement
        WHERE transaction_date BETWEEN ? AND ?
        GROUP BY merchant_id
        HAVING sum(settlement_gap) > 0
        ORDER BY settlement_gap DESC
        LIMIT ?
    """
    rows = _fetch(sql, [start, end, limit])
    for row in rows:
        row["settlement_gap"] = float(row["settlement_gap"])
    return rows


def data_quality_summary(limit: int) -> list[dict]:
    sql = """
        SELECT rule_id, source_table, severity, disposition, sum(failed_rows) AS failed_rows
        FROM ctl.dq_rule_result
        GROUP BY rule_id, source_table, severity, disposition
        HAVING sum(failed_rows) > 0
        ORDER BY failed_rows DESC
        LIMIT ?
    """
    rows = _fetch(sql, [limit])
    for row in rows:
        row["failed_rows"] = int(row["failed_rows"])
    return rows


def readiness() -> dict:
    gold_rows = _fetch("SELECT count(*) AS c FROM gold.agg_daily_merchant_settlement", [])[0]["c"]
    run = _fetch("SELECT batch_id, status FROM ctl.pipeline_run ORDER BY started_at DESC LIMIT 1", [])
    return {
        "gold_rows": int(gold_rows),
        "last_batch_id": run[0]["batch_id"] if run else None,
        "last_run_status": run[0]["status"] if run else None,
    }
