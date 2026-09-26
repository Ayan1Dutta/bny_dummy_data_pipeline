from __future__ import annotations

from decimal import Decimal


def one(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()


def test_successful_transaction_is_loaded_and_marked(con):
    row = one(con, "SELECT status, amount, is_successful FROM gold.fact_transaction WHERE transaction_id = 'T2'")
    assert row[0] == "SUCCESS"
    assert row[1] == Decimal("10000.00")
    assert row[2] is True


def test_failed_transaction_is_excluded_from_settlement_scope(con):
    assert one(con, "SELECT is_successful FROM gold.fact_transaction WHERE transaction_id = 'T4'")[0] is False
    assert one(con, "SELECT count(*) FROM gold.fact_settlement_reconciliation WHERE transaction_id = 'T4'")[0] == 0


def test_duplicate_transaction_id_is_deduplicated_keeping_latest(con):
    assert one(con, "SELECT count(*) FROM silver.transactions WHERE transaction_id = 'T2'")[0] == 1
    assert one(con, "SELECT payment_channel FROM silver.transactions WHERE transaction_id = 'T2'")[0] == "QR"


def test_duplicate_event_id_is_deduplicated_keeping_earliest_ingestion(con):
    assert one(con, "SELECT count(*) FROM silver.payment_events WHERE event_id = 'E1'")[0] == 1
    assert str(one(con, "SELECT ingestion_ts FROM silver.payment_events WHERE event_id = 'E1'")[0]).endswith("10:00:05")
    assert one(con, "SELECT failed_rows FROM ctl.dq_rule_result WHERE rule_id = 'EVT-004'")[0] == 1


def test_missing_merchant_is_quarantined_not_dropped(con):
    assert one(con, "SELECT count(*) FROM silver.transactions WHERE transaction_id = 'T6'")[0] == 0
    row = one(con, "SELECT rule_id, disposition FROM ctl.dq_quarantine WHERE business_key = 'T6'")
    assert row == ("TXN-005", "QUARANTINE")


def test_unknown_merchant_is_loaded_against_the_unknown_dimension_member(con):
    assert one(con, "SELECT merchant_key FROM gold.fact_transaction WHERE transaction_id = 'T8'")[0] == -1
    assert one(con, "SELECT is_merchant_known FROM silver.transactions WHERE transaction_id = 'T8'")[0] is False


def test_unmatched_settlement_is_flagged_orphan_and_excluded_from_kpis(con):
    assert one(con, "SELECT is_orphan FROM silver.settlements WHERE settlement_id = 'S5'")[0] is True
    assert one(con, "SELECT count(*) FROM ctl.dq_quarantine WHERE business_key = 'S5' AND rule_id = 'STL-006'")[0] == 1
    settled = one(
        con,
        "SELECT coalesce(sum(settled_amount), 0) FROM gold.fact_settlement_reconciliation WHERE transaction_id = 'T99'",
    )[0]
    assert settled == 0


def test_negative_settlement_is_quarantined(con):
    assert one(con, "SELECT count(*) FROM silver.settlements WHERE settlement_id = 'S8'")[0] == 0
    row = one(con, "SELECT rule_id, disposition FROM ctl.dq_quarantine WHERE business_key = 'S8'")
    assert row == ("STL-004", "QUARANTINE")


def test_negative_transaction_amount_is_rejected(con):
    assert one(con, "SELECT count(*) FROM silver.transactions WHERE transaction_id = 'T7'")[0] == 0
    assert one(con, "SELECT rule_id FROM ctl.dq_quarantine WHERE business_key = 'T7'")[0] == "TXN-004"


def test_invalid_currency_is_quarantined(con):
    assert one(con, "SELECT rule_id FROM ctl.dq_quarantine WHERE business_key = 'T9'")[0] == "TXN-006"


def test_late_arriving_event_is_flagged_on_business_time(con):
    row = one(con, "SELECT ingestion_lag_sec, is_late_arriving, is_sla_breach FROM silver.payment_events WHERE event_id = 'E2'")
    assert row[0] == 511
    assert row[1] is True
    assert row[2] is False
    breach = one(con, "SELECT is_sla_breach FROM silver.payment_events WHERE event_id = 'E3'")[0]
    assert breach is True


def test_out_of_order_events_are_ordered_by_business_time(con):
    rows = con.execute(
        """
        SELECT event_type FROM silver.payment_events
        WHERE transaction_id = 'T3' ORDER BY event_ts
        """
    ).fetchall()
    assert [r[0] for r in rows] == ["CREATED", "AUTHORIZED"]
    arrival = con.execute(
        "SELECT event_type FROM silver.payment_events WHERE transaction_id = 'T3' ORDER BY ingestion_ts"
    ).fetchall()
    assert [r[0] for r in arrival] == ["AUTHORIZED", "CREATED"]


def test_one_to_many_settlement_is_aggregated_before_transaction_level_maths(con):
    row = one(
        con,
        """
        SELECT settlement_count, settled_amount, transaction_amount, settlement_gap, settlement_state
        FROM gold.fact_settlement_reconciliation WHERE transaction_id = 'T2'
        """,
    )
    assert row[0] == 2
    assert row[1] == Decimal("10000.00")
    assert row[2] == Decimal("10000.00")
    assert row[3] == Decimal("0.00")
    assert row[4] == "FULLY_SETTLED"


def test_settlement_states_cover_pending_partial_and_unsettled(con):
    states = dict(
        con.execute(
            "SELECT transaction_id, settlement_state FROM gold.fact_settlement_reconciliation"
        ).fetchall()
    )
    assert states["T5"] == "UNSETTLED"
    assert states["T10"] == "PENDING"
    assert states["T11"] == "PARTIALLY_SETTLED"
    assert states["T3"] == "FULLY_SETTLED"


def test_sla_is_measured_from_transaction_to_first_settled_record(con):
    fast = one(con, "SELECT minutes_to_settle, is_sla_met FROM gold.fact_settlement_reconciliation WHERE transaction_id = 'T2'")
    slow = one(con, "SELECT minutes_to_settle, is_sla_met FROM gold.fact_settlement_reconciliation WHERE transaction_id = 'T3'")
    unsettled = one(con, "SELECT is_sla_met FROM gold.fact_settlement_reconciliation WHERE transaction_id = 'T5'")
    assert fast == (5.0, True)
    assert slow == (60.0, False)
    assert unsettled[0] is None


def test_merchant_risk_is_applied_as_of_the_transaction_date(con):
    feb = one(
        con,
        """
        SELECT m.risk_level FROM gold.fact_transaction f
        JOIN gold.dim_merchant m ON m.merchant_key = f.merchant_key
        WHERE f.transaction_id = 'T1'
        """,
    )
    sep = one(
        con,
        """
        SELECT m.risk_level FROM gold.fact_transaction f
        JOIN gold.dim_merchant m ON m.merchant_key = f.merchant_key
        WHERE f.transaction_id = 'T2'
        """,
    )
    assert feb[0] == "LOW"
    assert sep[0] == "MEDIUM"


def test_daily_aggregate_matches_hand_calculated_kpis(con):
    row = one(
        con,
        """
        SELECT sum(transaction_amount), sum(settled_amount), sum(settlement_gap),
               sum(sla_eligible_count), sum(sla_met_count), sum(unsettled_count), sum(success_count)
        FROM gold.agg_daily_merchant_settlement
        WHERE transaction_date = DATE '2026-09-01'
        """,
    )
    assert row[0] == Decimal("36000.00")
    assert row[1] == Decimal("20000.00")
    assert row[2] == Decimal("16000.00")
    assert row[3] == 3
    assert row[4] == 2
    assert row[5] == 2
    assert row[6] == 6


def test_pipeline_rerun_is_idempotent(tmp_path):
    from src.pipeline import run_pipeline
    from tests.conftest import FILES

    raw = tmp_path / "raw"
    raw.mkdir()
    for name, content in FILES.items():
        (raw / name).write_text(content, encoding="utf-8")
    warehouse = str(tmp_path / "wh.duckdb")

    run_pipeline.run(raw_dir=str(raw), warehouse=warehouse)
    before = _snapshot(warehouse)
    summary = run_pipeline.run(raw_dir=str(raw), warehouse=warehouse)
    after = _snapshot(warehouse)

    assert summary["silver"]["transactions"]["delta_rows"] == 0
    assert summary["gold"]["fact_transaction_delta"] == 0
    assert before == after


def test_incremental_run_only_processes_new_records(tmp_path):
    from src.pipeline import run_pipeline
    from tests.conftest import FILES

    raw = tmp_path / "raw"
    raw.mkdir()
    for name, content in FILES.items():
        (raw / name).write_text(content, encoding="utf-8")
    warehouse = str(tmp_path / "wh.duckdb")
    run_pipeline.run(raw_dir=str(raw), warehouse=warehouse)

    (raw / "transactions_day2.csv").write_text(
        "transaction_id,merchant_id,customer_id,transaction_ts,amount,currency,status,payment_channel\n"
        "T20,M200,C20,2026-09-02 10:00:00,2500.00,INR,SUCCESS,POS\n",
        encoding="utf-8",
    )
    (raw / "settlements_day2.csv").write_text(
        "settlement_id,transaction_id,settlement_ts,settlement_amount,settlement_status,settlement_batch\n"
        "S20,T20,2026-09-02 10:12:00,2500.00,SETTLED,B9\n",
        encoding="utf-8",
    )
    summary = run_pipeline.run(raw_dir=str(raw), warehouse=warehouse)

    assert summary["silver"]["transactions"]["delta_rows"] == 1
    assert summary["gold"]["affected_transactions"] == 1
    assert summary["gold"]["agg_rows"] > 0
    assert _day2(warehouse) == (2500.0, 2500.0)


def test_late_arriving_settlement_reopens_a_closed_day(tmp_path):
    from src.pipeline import run_pipeline
    from tests.conftest import FILES

    raw = tmp_path / "raw"
    raw.mkdir()
    for name, content in FILES.items():
        (raw / name).write_text(content, encoding="utf-8")
    warehouse = str(tmp_path / "wh.duckdb")
    run_pipeline.run(raw_dir=str(raw), warehouse=warehouse)
    assert _state(warehouse, "T5") == "UNSETTLED"

    (raw / "settlements_late.csv").write_text(
        "settlement_id,transaction_id,settlement_ts,settlement_amount,settlement_status,settlement_batch\n"
        "S30,T5,2026-09-03 09:00:00,3000.00,SETTLED,B30\n",
        encoding="utf-8",
    )
    summary = run_pipeline.run(raw_dir=str(raw), warehouse=warehouse)

    assert summary["gold"]["affected_transactions"] == 1
    assert _state(warehouse, "T5") == "FULLY_SETTLED"
    assert _settled_on(warehouse, "2026-09-01") == 23000.0


def _state(warehouse: str, transaction_id: str) -> str:
    from src import db

    with db.session(warehouse) as con:
        return con.execute(
            "SELECT settlement_state FROM gold.fact_settlement_reconciliation WHERE transaction_id = ?",
            [transaction_id],
        ).fetchone()[0]


def _settled_on(warehouse: str, day: str) -> float:
    from src import db

    with db.session(warehouse) as con:
        return float(
            con.execute(
                "SELECT sum(settled_amount) FROM gold.agg_daily_merchant_settlement WHERE transaction_date = ?",
                [day],
            ).fetchone()[0]
        )


def _day2(warehouse: str) -> tuple:
    from src import db

    with db.session(warehouse) as con:
        row = con.execute(
            """
            SELECT sum(transaction_amount), sum(settled_amount)
            FROM gold.agg_daily_merchant_settlement WHERE transaction_date = DATE '2026-09-02'
            """
        ).fetchone()
        return (float(row[0]), float(row[1]))


def _snapshot(warehouse: str) -> tuple:
    from src import db

    with db.session(warehouse) as con:
        return (
            con.execute("SELECT count(*) FROM silver.transactions").fetchone()[0],
            con.execute("SELECT count(*) FROM gold.fact_transaction").fetchone()[0],
            con.execute("SELECT count(*) FROM gold.fact_settlement_reconciliation").fetchone()[0],
            con.execute("SELECT coalesce(sum(settled_amount), 0) FROM gold.agg_daily_merchant_settlement").fetchone()[0],
        )
