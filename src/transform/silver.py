from __future__ import annotations

from datetime import datetime

import duckdb

from src.config import settings, utcnow
from src.quality import rules as R

EPOCH = datetime(1970, 1, 1)


def get_watermark(con: duckdb.DuckDBPyConnection, target: str) -> datetime:
    row = con.execute("SELECT last_ingested_at FROM ctl.etl_watermark WHERE target_table = ?", [target]).fetchone()
    return row[0] if row else EPOCH


def set_watermark(con: duckdb.DuckDBPyConnection, target: str, value: datetime | None, batch_id: str, rows: int) -> None:
    if value is None:
        return
    con.execute(
        """
        INSERT OR REPLACE INTO ctl.etl_watermark (target_table, last_ingested_at, last_batch_id, rows_processed, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [target, value, batch_id, rows, utcnow()],
    )


def _record_rule_results(con: duckdb.DuckDBPyConnection, stg: str, table: str, batch_id: str) -> None:
    total = con.execute(f"SELECT count(*) FROM {stg}").fetchone()[0]
    for rule in R.BY_TABLE[table]:
        try:
            failed = con.execute(f"SELECT count(*) FROM {stg} WHERE {rule.predicate}").fetchone()[0]
        except duckdb.Error:
            continue
        con.execute(
            """
            INSERT OR REPLACE INTO ctl.dq_rule_result
                (batch_id, rule_id, source_table, severity, disposition, failed_rows, total_rows, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [batch_id, rule.rule_id, table, rule.severity, rule.disposition, failed, total, utcnow()],
        )


def _blocking_case(table: str) -> tuple[str, str]:
    blocking = R.blocking_rules(table)
    case_rule = "CASE " + " ".join(f"WHEN {r.predicate} THEN '{r.rule_id}'" for r in blocking) + " END"
    case_reason = "CASE " + " ".join(
        f"WHEN {r.predicate} THEN '{r.description.replace(chr(39), '')}'" for r in blocking
    ) + " END"
    where = " OR ".join(f"({r.predicate})" for r in blocking)
    return case_rule, case_reason, where


def _quarantine(con: duckdb.DuckDBPyConnection, stg: str, table: str, key_expr: str, batch_id: str) -> int:
    case_rule, case_reason, where = _blocking_case(table)
    severity_case = "CASE " + " ".join(
        f"WHEN {r.predicate} THEN '{r.severity}'" for r in R.blocking_rules(table)
    ) + " END"
    disposition_case = "CASE " + " ".join(
        f"WHEN {r.predicate} THEN '{r.disposition}'" for r in R.blocking_rules(table)
    ) + " END"
    con.execute(
        f"""
        INSERT OR REPLACE INTO ctl.dq_quarantine
        SELECT md5(concat_ws('|', ?, coalesce({key_expr}, 'NA'), CAST(_source_row_num AS VARCHAR), ?)) AS quarantine_id,
               ? AS source_table,
               {key_expr} AS business_key,
               {case_rule} AS rule_id,
               {severity_case} AS severity,
               {disposition_case} AS disposition,
               {case_reason} AS reason,
               CAST(to_json(t) AS VARCHAR) AS record_payload,
               ? AS batch_id,
               now()::TIMESTAMP AS detected_at
        FROM {stg} t
        WHERE {where}
        """,
        [table, batch_id, table, batch_id],
    )
    return con.execute(f"SELECT count(*) FROM {stg} t WHERE {where}").fetchone()[0]


def _business_exception(con: duckdb.DuckDBPyConnection, stg: str, table: str, key_expr: str, rule_id: str, batch_id: str) -> None:
    rule = R.rule(rule_id)
    con.execute(
        f"""
        INSERT OR REPLACE INTO ctl.dq_quarantine
        SELECT md5(concat_ws('|', ?, coalesce({key_expr}, 'NA'), ?)) AS quarantine_id,
               ? AS source_table,
               {key_expr} AS business_key,
               ? AS rule_id,
               ? AS severity,
               ? AS disposition,
               ? AS reason,
               CAST(to_json(t) AS VARCHAR) AS record_payload,
               ? AS batch_id,
               now()::TIMESTAMP AS detected_at
        FROM {stg} t
        WHERE {rule.predicate}
        """,
        [table, rule_id, table, rule_id, rule.severity, rule.disposition, rule.description, batch_id],
    )


def load_merchant(con: duckdb.DuckDBPyConnection, batch_id: str) -> dict:
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE stg_merchant AS
        WITH src AS (
            SELECT *, row_number() OVER (
                       PARTITION BY trim(merchant_id), trim(effective_from)
                       ORDER BY _ingested_at DESC, _source_row_num DESC) AS dup_rank
            FROM bronze.merchant
        ),
        typed AS (
            SELECT trim(merchant_id) AS merchant_id,
                   coalesce(nullif(trim(merchant_name), ''), 'UNKNOWN') AS merchant_name,
                   upper(trim(coalesce(merchant_category, ''))) AS merchant_category,
                   upper(trim(coalesce(country, ''))) AS country,
                   upper(trim(coalesce(risk_level, ''))) AS risk_level,
                   TRY_CAST(effective_from AS DATE) AS eff_from_typed,
                   coalesce(TRY_CAST(nullif(trim(effective_to), '') AS DATE), DATE '9999-12-31') AS eff_to_typed,
                   dup_rank, _batch_id, _ingested_at, _source_file, _source_row_num
            FROM src
        )
        SELECT t.*,
               EXISTS (SELECT 1 FROM typed o
                       WHERE o.merchant_id = t.merchant_id
                         AND o.eff_from_typed IS NOT NULL AND t.eff_from_typed IS NOT NULL
                         AND o.eff_from_typed <> t.eff_from_typed
                         AND o.eff_from_typed <= t.eff_to_typed
                         AND o.eff_to_typed >= t.eff_from_typed) AS has_overlap
        FROM typed t
        """
    )
    _record_rule_results(con, "stg_merchant", "merchant", batch_id)
    quarantined = _quarantine(con, "stg_merchant", "merchant", "merchant_id", batch_id)
    _business_exception(con, "stg_merchant", "merchant", "merchant_id", "MER-004", batch_id)

    _, _, where = _blocking_case("merchant")
    con.execute(
        f"""
        INSERT OR REPLACE INTO silver.merchant
        SELECT merchant_id, merchant_name, merchant_category, country, risk_level,
               eff_from_typed, eff_to_typed,
               (eff_to_typed = DATE '9999-12-31') AS is_current,
               _batch_id, _ingested_at
        FROM stg_merchant t
        WHERE dup_rank = 1 AND NOT ({where})
        """
    )
    loaded = con.execute("SELECT count(*) FROM silver.merchant").fetchone()[0]
    return {"loaded": loaded, "quarantined": quarantined}


def load_transactions(con: duckdb.DuckDBPyConnection, batch_id: str) -> dict:
    target = "silver.transactions"
    wm = get_watermark(con, target)
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE stg_txn AS
        WITH src AS (
            SELECT * FROM bronze.transactions WHERE _ingested_at > ?
        ),
        typed AS (
            SELECT trim(coalesce(transaction_id, '')) AS transaction_id,
                   nullif(trim(coalesce(merchant_id, '')), '') AS merchant_id,
                   nullif(trim(coalesce(customer_id, '')), '') AS customer_id,
                   TRY_CAST(transaction_ts AS TIMESTAMP) AS ts_typed,
                   TRY_CAST(amount AS DECIMAL(18,2)) AS amount_typed,
                   upper(trim(coalesce(currency, ''))) AS currency,
                   upper(trim(coalesce(status, ''))) AS status,
                   upper(trim(coalesce(payment_channel, ''))) AS payment_channel,
                   _batch_id, _ingested_at, _source_file, _source_row_num,
                   row_number() OVER (PARTITION BY trim(coalesce(transaction_id, ''))
                                      ORDER BY _ingested_at DESC, _source_row_num DESC) AS dup_rank
            FROM src
        )
        SELECT t.*,
               (m.merchant_id IS NOT NULL) AS is_merchant_known,
               md5(concat_ws('|', ?, coalesce(t.customer_id, 'NA'))) AS customer_key_hash
        FROM typed t
        LEFT JOIN (SELECT DISTINCT merchant_id FROM silver.merchant) m ON m.merchant_id = t.merchant_id
        """,
        [wm, settings.pii_salt],
    )
    _record_rule_results(con, "stg_txn", "transactions", batch_id)
    quarantined = _quarantine(con, "stg_txn", "transactions", "transaction_id", batch_id)

    _, _, where = _blocking_case("transactions")
    con.execute(
        f"""
        INSERT OR REPLACE INTO silver.transactions
        SELECT transaction_id, merchant_id, customer_id, customer_key_hash,
               ts_typed, CAST(ts_typed AS DATE), amount_typed, currency, status, payment_channel,
               is_merchant_known, _batch_id, _ingested_at
        FROM stg_txn t
        WHERE dup_rank = 1 AND NOT ({where})
        """
    )
    loaded = con.execute(f"SELECT count(*) FROM stg_txn t WHERE dup_rank = 1 AND NOT ({where})").fetchone()[0]
    high = con.execute("SELECT max(_ingested_at) FROM stg_txn").fetchone()[0]
    set_watermark(con, target, high, batch_id, loaded)
    return {"loaded": loaded, "quarantined": quarantined, "delta_rows": con.execute("SELECT count(*) FROM stg_txn").fetchone()[0]}


def load_settlements(con: duckdb.DuckDBPyConnection, batch_id: str) -> dict:
    target = "silver.settlements"
    wm = get_watermark(con, target)
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE stg_stl AS
        WITH src AS (
            SELECT * FROM bronze.settlements WHERE _ingested_at > ?
        ),
        typed AS (
            SELECT trim(coalesce(settlement_id, '')) AS settlement_id,
                   trim(coalesce(transaction_id, '')) AS transaction_id,
                   TRY_CAST(settlement_ts AS TIMESTAMP) AS ts_typed,
                   TRY_CAST(settlement_amount AS DECIMAL(18,2)) AS amount_typed,
                   upper(trim(coalesce(settlement_status, ''))) AS settlement_status,
                   nullif(trim(coalesce(settlement_batch, '')), '') AS settlement_batch,
                   _batch_id, _ingested_at, _source_file, _source_row_num,
                   row_number() OVER (PARTITION BY trim(coalesce(settlement_id, ''))
                                      ORDER BY _ingested_at DESC, _source_row_num DESC) AS dup_rank
            FROM src
        )
        SELECT t.*, (x.transaction_id IS NULL) AS is_orphan
        FROM typed t
        LEFT JOIN silver.transactions x ON x.transaction_id = t.transaction_id
        """,
        [wm],
    )
    _record_rule_results(con, "stg_stl", "settlements", batch_id)
    quarantined = _quarantine(con, "stg_stl", "settlements", "settlement_id", batch_id)
    _business_exception(con, "stg_stl", "settlements", "settlement_id", "STL-006", batch_id)

    _, _, where = _blocking_case("settlements")
    con.execute(
        f"""
        INSERT OR REPLACE INTO silver.settlements
        SELECT settlement_id, transaction_id, ts_typed, CAST(ts_typed AS DATE),
               amount_typed, settlement_status, settlement_batch, is_orphan, _batch_id, _ingested_at
        FROM stg_stl t
        WHERE dup_rank = 1 AND NOT ({where})
        """
    )
    con.execute(
        """
        UPDATE silver.settlements s
        SET is_orphan = NOT EXISTS (SELECT 1 FROM silver.transactions t WHERE t.transaction_id = s.transaction_id)
        WHERE s.is_orphan <> NOT EXISTS (SELECT 1 FROM silver.transactions t WHERE t.transaction_id = s.transaction_id)
        """
    )
    loaded = con.execute(f"SELECT count(*) FROM stg_stl t WHERE dup_rank = 1 AND NOT ({where})").fetchone()[0]
    high = con.execute("SELECT max(_ingested_at) FROM stg_stl").fetchone()[0]
    set_watermark(con, target, high, batch_id, loaded)
    return {"loaded": loaded, "quarantined": quarantined, "delta_rows": con.execute("SELECT count(*) FROM stg_stl").fetchone()[0]}


def load_payment_events(con: duckdb.DuckDBPyConnection, batch_id: str) -> dict:
    target = "silver.payment_events"
    wm = get_watermark(con, target)
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE stg_evt AS
        WITH src AS (
            SELECT * FROM bronze.payment_events WHERE _ingested_at > ?
        ),
        typed AS (
            SELECT trim(coalesce(event_id, '')) AS event_id,
                   trim(coalesce(transaction_id, '')) AS transaction_id,
                   upper(trim(coalesce(event_type, ''))) AS event_type,
                   TRY_CAST(event_ts AS TIMESTAMP) AS event_ts_typed,
                   TRY_CAST(ingestion_ts AS TIMESTAMP) AS ingestion_ts_typed,
                   TRY_CAST(processing_ms AS INTEGER) AS processing_ms,
                   _batch_id, _ingested_at, _source_file, _source_row_num,
                   row_number() OVER (PARTITION BY trim(coalesce(event_id, ''))
                                      ORDER BY TRY_CAST(ingestion_ts AS TIMESTAMP) ASC, _ingested_at ASC) AS dup_rank
            FROM src
        )
        SELECT t.*,
               date_diff('second', event_ts_typed, ingestion_ts_typed)::DOUBLE AS ingestion_lag_sec,
               (date_diff('second', event_ts_typed, ingestion_ts_typed) > ?) AS is_late_arriving,
               (date_diff('second', event_ts_typed, ingestion_ts_typed) > ?) AS is_sla_breach
        FROM typed t
        """,
        [wm, settings.late_event_threshold_sec, settings.event_sla_threshold_sec],
    )
    _record_rule_results(con, "stg_evt", "payment_events", batch_id)
    quarantined = _quarantine(con, "stg_evt", "payment_events", "event_id", batch_id)
    _business_exception(con, "stg_evt", "payment_events", "event_id", "EVT-006", batch_id)

    _, _, where = _blocking_case("payment_events")
    con.execute(
        f"""
        INSERT OR REPLACE INTO silver.payment_events
        SELECT event_id, transaction_id, event_type, event_ts_typed, ingestion_ts_typed,
               CAST(event_ts_typed AS DATE), processing_ms,
               coalesce(ingestion_lag_sec, 0), coalesce(is_late_arriving, FALSE), coalesce(is_sla_breach, FALSE),
               _batch_id, _ingested_at
        FROM stg_evt t
        WHERE dup_rank = 1 AND NOT ({where})
        """
    )
    loaded = con.execute(f"SELECT count(*) FROM stg_evt t WHERE dup_rank = 1 AND NOT ({where})").fetchone()[0]
    high = con.execute("SELECT max(_ingested_at) FROM stg_evt").fetchone()[0]
    set_watermark(con, target, high, batch_id, loaded)
    return {"loaded": loaded, "quarantined": quarantined, "delta_rows": con.execute("SELECT count(*) FROM stg_evt").fetchone()[0]}


def run(con: duckdb.DuckDBPyConnection, batch_id: str) -> dict:
    result = {
        "merchant": load_merchant(con, batch_id),
        "transactions": load_transactions(con, batch_id),
        "settlements": load_settlements(con, batch_id),
        "payment_events": load_payment_events(con, batch_id),
    }
    result["silver_rows"] = sum(v["loaded"] for v in result.values() if isinstance(v, dict))
    result["quarantined_rows"] = sum(v["quarantined"] for v in result.values() if isinstance(v, dict))
    return result
