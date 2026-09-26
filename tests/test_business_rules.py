from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from src.api import repository
from src.config import settings
from src.pipeline import run_pipeline
from tests.conftest import MERCHANTS

WINDOW = {"start_date": "2026-09-01", "end_date": "2026-09-07"}

SPLIT_TRANSACTIONS = """transaction_id,merchant_id,customer_id,transaction_ts,amount,currency,status,payment_channel
T1,M100,C1,2026-09-01 10:00:00,9000.00,INR,SUCCESS,POS
T2,M200,C2,2026-09-01 11:00:00,1000.00,INR,SUCCESS,QR
"""

SPLIT_SETTLEMENTS = """settlement_id,transaction_id,settlement_ts,settlement_amount,settlement_status,settlement_batch
S1,T1,2026-09-01 10:05:00,3000.00,SETTLED,B1
S2,T1,2026-09-01 10:10:00,3000.00,SETTLED,B1
S3,T1,2026-09-01 10:15:00,3000.00,SETTLED,B2
S4,T2,2026-09-01 11:05:00,1000.00,SETTLED,B2
"""

SPLIT_EVENTS = "event_id,transaction_id,event_type,event_ts,ingestion_ts,processing_ms\n"


@pytest.fixture()
def authed(client):
    from src.api.main import app

    with TestClient(app, headers={"X-API-Key": settings.api_key}) as c:
        yield c


@pytest.fixture()
def split_settlement_api(tmp_path, warehouse):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "merchant.csv").write_text(MERCHANTS, encoding="utf-8")
    (raw / "transactions.csv").write_text(SPLIT_TRANSACTIONS, encoding="utf-8")
    (raw / "settlements.csv").write_text(SPLIT_SETTLEMENTS, encoding="utf-8")
    (raw / "payment_events.csv").write_text(SPLIT_EVENTS, encoding="utf-8")
    target = str(tmp_path / "split.duckdb")
    run_pipeline.run(raw_dir=str(raw), warehouse=target)

    from src.api.main import app

    repository.set_warehouse(target)
    try:
        with TestClient(app, headers={"X-API-Key": settings.api_key}) as c:
            yield c, target
    finally:
        repository.set_warehouse(warehouse)


def test_mandatory_negative_three_settlements_do_not_triple_the_settlement_rate(split_settlement_api):
    client, target = split_settlement_api
    import duckdb

    con = duckdb.connect(target, read_only=True)
    try:
        legs = con.execute("SELECT count(*) FROM gold.fact_settlement WHERE transaction_id = 'T1'").fetchone()[0]
        naive_joined_amount = con.execute(
            """
            SELECT sum(t.amount) FROM gold.fact_transaction t
            JOIN gold.fact_settlement s ON s.transaction_id = t.transaction_id
            """
        ).fetchone()[0]
        naive_settled_rows = con.execute(
            """
            SELECT sum(t.amount) FROM gold.fact_transaction t
            JOIN gold.fact_settlement s ON s.transaction_id = t.transaction_id
            WHERE s.settlement_status = 'SETTLED'
            """
        ).fetchone()[0]
    finally:
        con.close()

    assert legs == 3, "precondition: the scenario must contain one transaction with multiple settlement records"
    assert naive_joined_amount == Decimal("28000.00"), "precondition: a join before aggregation really does fan out"
    assert 100.0 * float(naive_settled_rows) / 10000.0 == 280.0

    body = client.get("/api/v1/settlement-summary", params=WINDOW).json()
    assert body["transaction_amount"] == 10000.0
    assert body["settled_amount"] == 10000.0
    assert body["settlement_rate"] == 100.0
    assert body["settlement_gap"] == 0.0
    assert body["transaction_count"] == 2

    m100 = client.get("/api/v1/settlement-summary", params={**WINDOW, "merchant_id": "M100"}).json()
    assert m100["settled_amount"] == m100["transaction_amount"] == 9000.0
    assert m100["settlement_rate"] == 100.0


def test_mandatory_negative_split_settlement_in_shared_fixture_is_counted_once(con, authed):
    legs = con.execute(
        "SELECT count(*) FROM gold.fact_settlement WHERE transaction_id = 'T2' AND settlement_status = 'SETTLED'"
    ).fetchone()[0]
    assert legs >= 2, "precondition: regression data must keep a multi-settlement transaction"

    body = authed.get("/api/v1/settlement-summary", params=WINDOW).json()
    assert body["settled_amount"] == 20000.0
    assert body["settlement_rate"] == 55.56
    assert body["settled_amount"] <= body["transaction_amount"]


def test_no_merchant_ever_reports_settled_above_transacted(con, authed):
    merchants = [r[0] for r in con.execute("SELECT DISTINCT merchant_id FROM gold.dim_merchant WHERE merchant_id <> 'UNKNOWN'").fetchall()]
    assert merchants
    for merchant_id in merchants:
        body = authed.get("/api/v1/settlement-summary", params={**WINDOW, "merchant_id": merchant_id}).json()
        assert body["settled_amount"] <= body["transaction_amount"], merchant_id
        assert 0 <= body["settlement_rate"] <= 100, merchant_id


def test_settlement_calculation_counts_only_settled_status(authed):
    body = authed.get("/api/v1/settlement-summary", params={**WINDOW, "merchant_id": "M100"}).json()
    assert body["transaction_amount"] == 24000.0
    assert body["settled_amount"] == 15000.0
    assert body["settlement_rate"] == 62.5


def test_settlement_gap_is_transacted_minus_settled(authed):
    body = authed.get("/api/v1/settlement-summary", params=WINDOW).json()
    assert body["settlement_gap"] == 16000.0
    assert body["settlement_gap"] == round(body["transaction_amount"] - body["settled_amount"], 2)


def test_sla_uses_first_settled_record_within_thirty_minutes(authed):
    assert authed.get("/api/v1/settlement-summary", params=WINDOW).json()["sla_rate"] == 66.67
    assert authed.get("/api/v1/settlement-summary", params={**WINDOW, "merchant_id": "M100"}).json()["sla_rate"] == 100.0
    assert authed.get("/api/v1/settlement-summary", params={**WINDOW, "merchant_id": "M200"}).json()["sla_rate"] == 0.0


def test_failed_and_reversed_payments_are_excluded_from_volume(authed):
    body = authed.get("/api/v1/settlement-summary", params=WINDOW).json()
    assert body["transaction_count"] == 6
    assert body["transaction_amount"] == 36000.0


def test_merchant_exception_uses_or_logic_between_thresholds(authed):
    rows = authed.get(
        "/api/v1/merchant-exceptions",
        params={**WINDOW, "settlement_rate_threshold": 60, "sla_rate_threshold": 50},
    ).json()
    assert [r["merchant_id"] for r in rows] == ["M200"]
    assert rows[0]["breach_reasons"] == ["sla_rate<50.0"]


def test_merchant_exception_default_thresholds_list_both_breaching_merchants(authed):
    rows = authed.get("/api/v1/merchant-exceptions", params=WINDOW).json()
    by_id = {r["merchant_id"]: r for r in rows}
    assert set(by_id) == {"M100", "M200"}
    assert by_id["M100"]["breach_reasons"] == ["settlement_rate<95.0"]
    assert by_id["M200"]["breach_reasons"] == ["settlement_rate<95.0", "sla_rate<90.0"]


def test_healthy_merchants_are_not_listed_as_exceptions(authed):
    rows = authed.get(
        "/api/v1/merchant-exceptions",
        params={**WINDOW, "settlement_rate_threshold": 0, "sla_rate_threshold": 0},
    ).json()
    assert rows == []


def test_unknown_merchant_is_never_reported_as_a_merchant_exception(authed):
    rows = authed.get("/api/v1/merchant-exceptions", params=WINDOW).json()
    assert "UNKNOWN" not in {r["merchant_id"] for r in rows}


def test_merchant_exception_shows_risk_as_at_period_end(authed):
    rows = authed.get("/api/v1/merchant-exceptions", params=WINDOW).json()
    assert {r["merchant_id"]: r["risk_level"] for r in rows} == {"M100": "MEDIUM", "M200": "LOW"}
