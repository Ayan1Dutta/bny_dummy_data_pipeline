from __future__ import annotations

import duckdb

from src.config import settings, utcnow
from src.transform.silver import get_watermark, set_watermark

UNKNOWN_MERCHANT_KEY = -1
MERCHANT_KEY = "CAST(hash(concat_ws('|', merchant_id, CAST(effective_from AS VARCHAR))) % 9000000000000 AS BIGINT)"
CUSTOMER_KEY = "CAST(hash(customer_key_hash) % 9000000000000 AS BIGINT)"


def build_dim_date(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO gold.dim_date
        WITH bounds AS (
            SELECT least(coalesce(min(transaction_date), DATE '2026-01-01'), DATE '2026-01-01') AS d0,
                   greatest(coalesce(max(transaction_date), DATE '2026-12-31'), DATE '2026-12-31') AS d1
            FROM silver.transactions
        ),
        days AS (
            SELECT unnest(generate_series(d0, d1, INTERVAL 1 DAY))::DATE AS full_date FROM bounds
        )
        SELECT CAST(strftime(full_date, '%Y%m%d') AS INTEGER),
               full_date,
               extract('day' FROM full_date),
               extract('month' FROM full_date),
               strftime(full_date, '%B'),
               extract('quarter' FROM full_date),
               extract('year' FROM full_date),
               strftime(full_date, '%A'),
               isodow(full_date) >= 6
        FROM days
        """
    )


def build_dim_merchant(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        INSERT OR REPLACE INTO gold.dim_merchant
        SELECT {MERCHANT_KEY}, merchant_id, merchant_name, merchant_category, country,
               risk_level, effective_from, effective_to, is_current
        FROM silver.merchant
        """
    )
    con.execute(
        """
        INSERT OR REPLACE INTO gold.dim_merchant
        VALUES (-1, 'UNKNOWN', 'Unknown merchant', 'UNKNOWN', 'UNKNOWN', 'UNKNOWN',
                DATE '1900-01-01', DATE '9999-12-31', TRUE)
        """
    )


def build_dim_customer(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        INSERT OR REPLACE INTO gold.dim_customer
        SELECT {CUSTOMER_KEY}, customer_key_hash, min(transaction_date), max(transaction_date)
        FROM silver.transactions
        GROUP BY customer_key_hash
        """
    )


def load_fact_transaction(con: duckdb.DuckDBPyConnection, batch_id: str) -> int:
    target = "gold.fact_transaction"
    wm = get_watermark(con, target)
    con.execute(
        f"""
        INSERT OR REPLACE INTO gold.fact_transaction
        SELECT t.transaction_id,
               CAST(strftime(t.transaction_date, '%Y%m%d') AS INTEGER),
               coalesce(m.merchant_key, {UNKNOWN_MERCHANT_KEY}),
               CAST(hash(t.customer_key_hash) % 9000000000000 AS BIGINT),
               t.transaction_ts, t.transaction_date, t.amount, t.currency, t.status, t.payment_channel,
               (t.status = 'SUCCESS'),
               ?, now()::TIMESTAMP
        FROM silver.transactions t
        LEFT JOIN gold.dim_merchant m
               ON m.merchant_id = t.merchant_id
              AND t.transaction_date BETWEEN m.effective_from AND m.effective_to
        WHERE t._ingested_at > ?
        """,
        [batch_id, wm],
    )
    rows = con.execute("SELECT count(*) FROM silver.transactions WHERE _ingested_at > ?", [wm]).fetchone()[0]
    high = con.execute("SELECT max(_ingested_at) FROM silver.transactions").fetchone()[0]
    set_watermark(con, target, high, batch_id, rows)
    return rows


def load_fact_settlement(con: duckdb.DuckDBPyConnection, batch_id: str) -> int:
    target = "gold.fact_settlement"
    wm = get_watermark(con, target)
    con.execute(
        """
        INSERT OR REPLACE INTO gold.fact_settlement
        SELECT s.settlement_id, s.transaction_id,
               CAST(strftime(s.settlement_date, '%Y%m%d') AS INTEGER),
               ft.merchant_key,
               s.settlement_ts, s.settlement_date, s.settlement_amount, s.settlement_status,
               s.settlement_batch, s.is_orphan, ?, now()::TIMESTAMP
        FROM silver.settlements s
        LEFT JOIN gold.fact_transaction ft ON ft.transaction_id = s.transaction_id
        WHERE s._ingested_at > ?
        """,
        [batch_id, wm],
    )
    rows = con.execute("SELECT count(*) FROM silver.settlements WHERE _ingested_at > ?", [wm]).fetchone()[0]
    high = con.execute("SELECT max(_ingested_at) FROM silver.settlements").fetchone()[0]
    set_watermark(con, target, high, batch_id, rows)
    return rows


def load_fact_payment_event(con: duckdb.DuckDBPyConnection, batch_id: str) -> int:
    target = "gold.fact_payment_event"
    wm = get_watermark(con, target)
    con.execute(
        """
        INSERT OR REPLACE INTO gold.fact_payment_event
        SELECT e.event_id, e.transaction_id,
               CAST(strftime(e.event_date, '%Y%m%d') AS INTEGER),
               e.event_type, e.event_ts, e.ingestion_ts, e.processing_ms,
               e.ingestion_lag_sec, e.is_late_arriving, e.is_sla_breach, ?, now()::TIMESTAMP
        FROM silver.payment_events e
        WHERE e._ingested_at > ?
        """,
        [batch_id, wm],
    )
    rows = con.execute("SELECT count(*) FROM silver.payment_events WHERE _ingested_at > ?", [wm]).fetchone()[0]
    high = con.execute("SELECT max(_ingested_at) FROM silver.payment_events").fetchone()[0]
    set_watermark(con, target, high, batch_id, rows)
    return rows


def affected_transactions(con: duckdb.DuckDBPyConnection, batch_id: str, full: bool) -> int:
    if full:
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE tmp_affected_txn AS
            SELECT transaction_id FROM gold.fact_transaction WHERE is_successful
            """
        )
    else:
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE tmp_affected_txn AS
            SELECT DISTINCT transaction_id FROM (
                SELECT transaction_id FROM gold.fact_transaction WHERE _batch_id = ? AND is_successful
                UNION
                SELECT transaction_id FROM gold.fact_settlement WHERE _batch_id = ?
            )
            """,
            [batch_id, batch_id],
        )
    return con.execute("SELECT count(*) FROM tmp_affected_txn").fetchone()[0]


def build_reconciliation(con: duckdb.DuckDBPyConnection, batch_id: str) -> int:
    con.execute("DELETE FROM gold.fact_settlement_reconciliation WHERE transaction_id IN (SELECT transaction_id FROM tmp_affected_txn)")
    con.execute(
        """
        INSERT INTO gold.fact_settlement_reconciliation
        WITH scope AS (
            SELECT ft.* FROM gold.fact_transaction ft
            JOIN tmp_affected_txn a ON a.transaction_id = ft.transaction_id
            WHERE ft.is_successful
        ),
        stl AS (
            SELECT s.transaction_id,
                   sum(CASE WHEN s.settlement_status = 'SETTLED' THEN s.settlement_amount ELSE 0 END) AS settled_amount,
                   sum(CASE WHEN s.settlement_status = 'PENDING' THEN s.settlement_amount ELSE 0 END) AS pending_amount,
                   sum(CASE WHEN s.settlement_status = 'FAILED'  THEN s.settlement_amount ELSE 0 END) AS failed_amount,
                   count(*) AS settlement_count,
                   min(CASE WHEN s.settlement_status = 'SETTLED' THEN s.settlement_ts END) AS first_settled_ts
            FROM gold.fact_settlement s
            JOIN tmp_affected_txn a ON a.transaction_id = s.transaction_id
            WHERE NOT s.is_orphan
            GROUP BY s.transaction_id
        )
        SELECT sc.transaction_id, sc.date_key, sc.merchant_key, sc.transaction_ts, sc.transaction_date,
               sc.amount,
               coalesce(stl.settled_amount, 0),
               coalesce(stl.pending_amount, 0),
               coalesce(stl.failed_amount, 0),
               sc.amount - coalesce(stl.settled_amount, 0),
               coalesce(stl.settlement_count, 0),
               stl.first_settled_ts,
               CASE WHEN stl.first_settled_ts IS NULL THEN NULL
                    ELSE date_diff('second', sc.transaction_ts, stl.first_settled_ts) / 60.0 END,
               CASE WHEN coalesce(stl.settlement_count, 0) = 0 THEN 'UNSETTLED'
                    WHEN coalesce(stl.settled_amount, 0) = 0 AND coalesce(stl.pending_amount, 0) > 0 THEN 'PENDING'
                    WHEN coalesce(stl.settled_amount, 0) = 0 THEN 'SETTLEMENT_FAILED'
                    WHEN coalesce(stl.settled_amount, 0) + ? >= sc.amount THEN 'FULLY_SETTLED'
                    ELSE 'PARTIALLY_SETTLED' END,
               CASE WHEN stl.first_settled_ts IS NULL THEN NULL
                    ELSE date_diff('second', sc.transaction_ts, stl.first_settled_ts) / 60.0 <= ? END,
               ?, now()::TIMESTAMP
        FROM scope sc
        LEFT JOIN stl ON stl.transaction_id = sc.transaction_id
        """,
        [settings.amount_tolerance, settings.settlement_sla_minutes, batch_id],
    )
    con.execute(
        """
        INSERT OR REPLACE INTO ctl.dq_quarantine
        SELECT md5(concat_ws('|', 'settlements', transaction_id, 'STL-008')),
               'settlements', transaction_id, 'STL-008', 'MEDIUM', 'BUSINESS_EXCEPTION',
               'settled amount exceeds the transaction amount - over-settlement',
               CAST(to_json(r) AS VARCHAR), ?, now()::TIMESTAMP
        FROM gold.fact_settlement_reconciliation r
        WHERE settled_amount > transaction_amount + ?
        """,
        [batch_id, settings.amount_tolerance],
    )
    return con.execute("SELECT count(*) FROM gold.fact_settlement_reconciliation").fetchone()[0]


def build_aggregate(con: duckdb.DuckDBPyConnection, batch_id: str) -> int:
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE tmp_affected_grain AS
        SELECT DISTINCT transaction_date, merchant_key
        FROM gold.fact_transaction
        WHERE transaction_id IN (SELECT transaction_id FROM tmp_affected_txn)
        """
    )
    con.execute(
        """
        DELETE FROM gold.agg_daily_merchant_settlement a
        WHERE EXISTS (SELECT 1 FROM tmp_affected_grain g
                      WHERE g.transaction_date = a.transaction_date AND g.merchant_key = a.merchant_key)
        """
    )
    con.execute(
        """
        INSERT INTO gold.agg_daily_merchant_settlement
        WITH base AS (
            SELECT ft.transaction_date, ft.merchant_key,
                   count(*) AS transaction_count,
                   count(*) FILTER (WHERE ft.is_successful) AS success_count,
                   sum(CASE WHEN ft.is_successful THEN ft.amount ELSE 0 END) AS transaction_amount
            FROM gold.fact_transaction ft
            JOIN tmp_affected_grain g
              ON g.transaction_date = ft.transaction_date AND g.merchant_key = ft.merchant_key
            GROUP BY 1, 2
        ),
        recon AS (
            SELECT r.transaction_date, r.merchant_key,
                   sum(r.settled_amount) AS settled_amount,
                   count(*) FILTER (WHERE r.settlement_state = 'UNSETTLED') AS unsettled_count,
                   count(*) FILTER (WHERE r.is_sla_met IS NOT NULL) AS sla_eligible_count,
                   count(*) FILTER (WHERE r.is_sla_met) AS sla_met_count
            FROM gold.fact_settlement_reconciliation r
            JOIN tmp_affected_grain g
              ON g.transaction_date = r.transaction_date AND g.merchant_key = r.merchant_key
            GROUP BY 1, 2
        )
        SELECT b.transaction_date, b.merchant_key, d.merchant_id, d.merchant_name, d.risk_level,
               b.transaction_count, b.success_count, b.transaction_amount,
               coalesce(r.settled_amount, 0),
               b.transaction_amount - coalesce(r.settled_amount, 0),
               coalesce(r.unsettled_count, 0),
               coalesce(r.sla_eligible_count, 0),
               coalesce(r.sla_met_count, 0),
               CASE WHEN b.transaction_amount = 0 THEN 0
                    ELSE round(100.0 * coalesce(r.settled_amount, 0) / b.transaction_amount, 4) END,
               CASE WHEN coalesce(r.sla_eligible_count, 0) = 0 THEN 0
                    ELSE round(100.0 * coalesce(r.sla_met_count, 0) / r.sla_eligible_count, 4) END,
               now()::TIMESTAMP
        FROM base b
        LEFT JOIN recon r ON r.transaction_date = b.transaction_date AND r.merchant_key = b.merchant_key
        JOIN gold.dim_merchant d ON d.merchant_key = b.merchant_key
        """
    )
    return con.execute("SELECT count(*) FROM gold.agg_daily_merchant_settlement").fetchone()[0]


def run(con: duckdb.DuckDBPyConnection, batch_id: str, full_refresh: bool = False) -> dict:
    build_dim_date(con)
    build_dim_merchant(con)
    build_dim_customer(con)
    txn = load_fact_transaction(con, batch_id)
    stl = load_fact_settlement(con, batch_id)
    evt = load_fact_payment_event(con, batch_id)
    affected = affected_transactions(con, batch_id, full_refresh)
    recon = build_reconciliation(con, batch_id)
    agg = build_aggregate(con, batch_id)
    return {
        "fact_transaction_delta": txn,
        "fact_settlement_delta": stl,
        "fact_payment_event_delta": evt,
        "affected_transactions": affected,
        "reconciliation_rows": recon,
        "agg_rows": agg,
        "completed_at": utcnow().isoformat(),
    }
