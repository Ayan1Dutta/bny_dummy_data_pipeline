from __future__ import annotations

WINDOW = {"start_date": "2026-09-01", "end_date": "2026-09-07"}


def test_health_and_readiness_are_public(client):
    assert client.get("/health").status_code == 200
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["warehouse_reachable"] is True


def test_settlement_summary_returns_200_for_a_valid_request(client, auth_headers):
    res = client.get("/api/v1/settlement-summary", params=WINDOW, headers=auth_headers)
    assert res.status_code == 200
    body = res.json()
    for field in ("transaction_count", "transaction_amount", "settled_amount", "settlement_rate", "settlement_gap", "sla_rate"):
        assert field in body


def test_settlement_summary_matches_expected_kpis(client, auth_headers):
    body = client.get("/api/v1/settlement-summary", params=WINDOW, headers=auth_headers).json()
    assert body["transaction_count"] == 6
    assert body["transaction_amount"] == 36000.0
    assert body["settled_amount"] == 20000.0
    assert body["settlement_gap"] == 16000.0
    assert body["settlement_rate"] == 55.56
    assert body["sla_rate"] == 66.67


def test_settlement_rate_is_bounded(client, auth_headers):
    body = client.get("/api/v1/settlement-summary", params=WINDOW, headers=auth_headers).json()
    assert 0 <= body["settlement_rate"] <= 100
    assert 0 <= body["sla_rate"] <= 100


def test_invalid_date_returns_400(client, auth_headers):
    res = client.get("/api/v1/settlement-summary", params={"start_date": "01-09-2026", "end_date": "2026-09-07"}, headers=auth_headers)
    assert res.status_code == 400


def test_inverted_date_range_returns_400(client, auth_headers):
    res = client.get("/api/v1/settlement-summary", params={"start_date": "2026-09-07", "end_date": "2026-09-01"}, headers=auth_headers)
    assert res.status_code == 400


def test_unknown_merchant_returns_404(client, auth_headers):
    res = client.get("/api/v1/settlement-summary", params={**WINDOW, "merchant_id": "M4242"}, headers=auth_headers)
    assert res.status_code == 404


def test_invalid_parameter_returns_422(client, auth_headers):
    assert client.get("/api/v1/merchant-exceptions", params={**WINDOW, "limit": 0}, headers=auth_headers).status_code == 422
    assert client.get("/api/v1/settlement-summary", params={**WINDOW, "merchant_id": "M100;DROP"}, headers=auth_headers).status_code == 422


def test_missing_required_parameter_returns_422(client, auth_headers):
    assert client.get("/api/v1/settlement-summary", params={"start_date": "2026-09-01"}, headers=auth_headers).status_code == 422


def test_missing_api_key_returns_401(client):
    assert client.get("/api/v1/settlement-summary", params=WINDOW).status_code == 401


def test_wrong_api_key_returns_401(client):
    res = client.get("/api/v1/settlement-summary", params=WINDOW, headers={"X-API-Key": "not-the-key"})
    assert res.status_code == 401


def test_merchant_filter_scopes_the_result(client, auth_headers):
    body = client.get("/api/v1/settlement-summary", params={**WINDOW, "merchant_id": "M200"}, headers=auth_headers).json()
    assert body["merchant_id"] == "M200"
    assert body["transaction_amount"] == 8000.0
    assert body["settled_amount"] == 5000.0


def test_merchant_exceptions_contract(client, auth_headers):
    rows = client.get("/api/v1/merchant-exceptions", params=WINDOW, headers=auth_headers).json()
    assert rows
    for row in rows:
        assert {"merchant_id", "merchant_name", "settlement_rate", "sla_rate", "risk_level"} <= set(row)
        assert 0 <= row["settlement_rate"] <= 100
        assert 0 <= row["sla_rate"] <= 100
        assert row["breach_reasons"]


def test_daily_trend_is_ordered_and_scoped(client, auth_headers):
    rows = client.get("/api/v1/daily-trend", params=WINDOW, headers=auth_headers).json()
    dates = [r["transaction_date"] for r in rows]
    assert dates == sorted(dates)
    assert all(WINDOW["start_date"] <= d <= WINDOW["end_date"] for d in dates)


def test_top_merchant_gaps_respects_limit_and_ordering(client, auth_headers):
    rows = client.get("/api/v1/top-merchant-gaps", params={**WINDOW, "limit": 2}, headers=auth_headers).json()
    assert len(rows) <= 2
    gaps = [r["settlement_gap"] for r in rows]
    assert gaps == sorted(gaps, reverse=True)


def test_openapi_contract_is_published(client):
    spec = client.get("/openapi.json").json()
    assert "/api/v1/settlement-summary" in spec["paths"]
    assert "/api/v1/merchant-exceptions" in spec["paths"]
    assert spec["info"]["version"]
