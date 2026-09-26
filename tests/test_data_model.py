from __future__ import annotations

import pytest

GRAIN_CHECKS = [
    ("gold.fact_transaction", "transaction_id"),
    ("gold.fact_settlement", "settlement_id"),
    ("gold.fact_payment_event", "event_id"),
    ("gold.fact_settlement_reconciliation", "transaction_id"),
    ("gold.dim_merchant", "merchant_key"),
    ("gold.dim_customer", "customer_key"),
    ("gold.dim_date", "date_key"),
]


@pytest.mark.parametrize("table,key", GRAIN_CHECKS)
def test_grain_is_unique(con, table, key):
    rows = con.execute(
        f"SELECT {key}, count(*) FROM {table} GROUP BY {key} HAVING count(*) > 1"
    ).fetchall()
    assert rows == []


def test_aggregate_grain_is_date_by_merchant(con):
    rows = con.execute(
        """
        SELECT transaction_date, merchant_key, count(*)
        FROM gold.agg_daily_merchant_settlement
        GROUP BY transaction_date, merchant_key HAVING count(*) > 1
        """
    ).fetchall()
    assert rows == []


def test_transactions_reference_an_existing_merchant_dimension_row(con):
    orphans = con.execute(
        """
        SELECT count(*) FROM gold.fact_transaction f
        LEFT JOIN gold.dim_merchant m ON m.merchant_key = f.merchant_key
        WHERE m.merchant_key IS NULL
        """
    ).fetchone()[0]
    assert orphans == 0


def test_transactions_reference_existing_date_and_customer(con):
    assert con.execute(
        """
        SELECT count(*) FROM gold.fact_transaction f
        LEFT JOIN gold.dim_date d ON d.date_key = f.date_key
        LEFT JOIN gold.dim_customer c ON c.customer_key = f.customer_key
        WHERE d.date_key IS NULL OR c.customer_key IS NULL
        """
    ).fetchone()[0] == 0


def test_non_orphan_settlements_reference_an_existing_transaction(con):
    assert con.execute(
        """
        SELECT count(*) FROM gold.fact_settlement s
        LEFT JOIN gold.fact_transaction t ON t.transaction_id = s.transaction_id
        WHERE NOT s.is_orphan AND t.transaction_id IS NULL
        """
    ).fetchone()[0] == 0


def test_reconciliation_covers_every_successful_transaction_exactly_once(con):
    missing = con.execute(
        """
        SELECT count(*) FROM gold.fact_transaction t
        LEFT JOIN gold.fact_settlement_reconciliation r ON r.transaction_id = t.transaction_id
        WHERE t.is_successful AND r.transaction_id IS NULL
        """
    ).fetchone()[0]
    extra = con.execute(
        """
        SELECT count(*) FROM gold.fact_settlement_reconciliation r
        LEFT JOIN gold.fact_transaction t ON t.transaction_id = r.transaction_id
        WHERE t.transaction_id IS NULL OR NOT t.is_successful
        """
    ).fetchone()[0]
    assert (missing, extra) == (0, 0)


def test_settlement_totals_reconcile_between_fact_and_reconciliation(con):
    fact_total = con.execute(
        """
        SELECT coalesce(sum(settlement_amount), 0) FROM gold.fact_settlement
        WHERE settlement_status = 'SETTLED' AND NOT is_orphan
        """
    ).fetchone()[0]
    recon_total = con.execute("SELECT coalesce(sum(settled_amount), 0) FROM gold.fact_settlement_reconciliation").fetchone()[0]
    assert fact_total == recon_total


def test_aggregate_totals_reconcile_with_the_reconciliation_fact(con):
    agg = con.execute(
        "SELECT sum(transaction_amount), sum(settled_amount) FROM gold.agg_daily_merchant_settlement"
    ).fetchone()
    recon = con.execute(
        "SELECT sum(transaction_amount), sum(settled_amount) FROM gold.fact_settlement_reconciliation"
    ).fetchone()
    assert agg == recon


def test_source_rows_are_either_loaded_or_recorded_as_exceptions(con):
    source = con.execute("SELECT count(*) FROM bronze.transactions").fetchone()[0]
    loaded = con.execute("SELECT count(*) FROM silver.transactions").fetchone()[0]
    quarantined = con.execute(
        "SELECT count(DISTINCT business_key) FROM ctl.dq_quarantine WHERE source_table = 'transactions' AND disposition IN ('REJECT','QUARANTINE')"
    ).fetchone()[0]
    deduped = con.execute("SELECT failed_rows FROM ctl.dq_rule_result WHERE rule_id = 'TXN-009'").fetchone()[0]
    assert source == loaded + quarantined + deduped


def test_settlement_gap_is_never_negative_at_aggregate_level(con):
    rows = con.execute(
        "SELECT count(*) FROM gold.agg_daily_merchant_settlement WHERE settlement_rate < 0 OR settlement_rate > 100.001"
    ).fetchone()[0]
    assert rows == 0


def test_scd2_merchant_windows_do_not_overlap(con):
    overlaps = con.execute(
        """
        SELECT count(*) FROM gold.dim_merchant a
        JOIN gold.dim_merchant b
          ON a.merchant_id = b.merchant_id AND a.effective_from < b.effective_from
        WHERE b.effective_from <= a.effective_to
        """
    ).fetchone()[0]
    assert overlaps == 0


def test_exactly_one_current_row_per_merchant(con):
    rows = con.execute(
        """
        SELECT merchant_id, count(*) FROM gold.dim_merchant
        WHERE is_current GROUP BY merchant_id HAVING count(*) <> 1
        """
    ).fetchall()
    assert rows == []
