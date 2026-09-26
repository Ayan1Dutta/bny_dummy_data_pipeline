from __future__ import annotations

import copy

import duckdb
import pytest
from fastapi.testclient import TestClient

from scripts import reconciliation_gate, smoke_test
from src.config import settings
from src.pipeline import run_pipeline
from tests.conftest import FILES

START, END = "2026-09-01", "2026-09-07"


class TamperedResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class InflatingClient:
    def __init__(self, inner: TestClient, factor: float):
        self.inner = inner
        self.factor = factor

    def get(self, path, params=None):
        response = self.inner.get(path, params=params)
        if path != "/api/v1/settlement-summary" or response.status_code != 200:
            return response
        body = response.json()
        body["settled_amount"] = round(body["settled_amount"] * self.factor, 2)
        body["settlement_gap"] = round(body["transaction_amount"] - body["settled_amount"], 2)
        if body["transaction_amount"]:
            body["settlement_rate"] = round(100 * body["settled_amount"] / body["transaction_amount"], 2)
        return TamperedResponse(200, body)


@pytest.fixture()
def authed(client):
    from src.api.main import app

    with TestClient(app, headers={"X-API-Key": settings.api_key}) as c:
        yield c


def test_latest_pipeline_run_succeeded(con):
    status, finished = con.execute(
        "SELECT status, finished_at FROM ctl.pipeline_run ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert status == "SUCCESS"
    assert finished is not None


def test_reconciliation_gate_passes_on_a_healthy_platform(warehouse, authed):
    report = reconciliation_gate.run(warehouse, START, END, client=authed)
    failed = [c for c in report["checks"] if c["status"] == "FAIL"]
    assert report["passed"], failed
    assert report["observed"]["settlement_rate"] == report["expected_from_facts"]["settlement_rate"] == 55.56


def test_reconciliation_gate_catches_inflated_settlement_while_http_is_200(warehouse, authed):
    report = reconciliation_gate.run(warehouse, START, END, client=InflatingClient(authed, 1.5))
    failed = {c["check"] for c in report["checks"] if c["status"] == "FAIL"}
    assert not report["passed"]
    assert "api_platform_matches_independent_recompute" in failed
    assert "api_merchant_settled_matches_facts" in failed


def test_reconciliation_gate_catches_a_rate_above_100_percent(warehouse, authed):
    report = reconciliation_gate.run(warehouse, START, END, client=InflatingClient(authed, 1.9))
    failed = {c["check"] for c in report["checks"] if c["status"] == "FAIL"}
    assert report["observed"]["settlement_rate"] > 100
    assert "api_platform_rate_within_bounds" in failed
    assert "api_merchant_settled_not_above_transacted" in failed


def test_reconciliation_gate_catches_a_fan_out_inside_the_warehouse(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    for name, content in FILES.items():
        (raw / name).write_text(content, encoding="utf-8")
    target = str(tmp_path / "wh.duckdb")
    run_pipeline.run(raw_dir=str(raw), warehouse=target)
    assert reconciliation_gate.run(target, START, END)["passed"]

    con = duckdb.connect(target)
    con.execute("UPDATE gold.agg_daily_merchant_settlement SET settled_amount = settled_amount * 2")
    con.close()

    report = reconciliation_gate.run(target, START, END)
    failed = {c["check"] for c in report["checks"] if c["status"] == "FAIL"}
    assert "aggregate_table_matches_independent_recompute" in failed


def test_baseline_drift_is_flagged_and_stable_data_is_not(warehouse):
    baseline = reconciliation_gate.expected_kpis(warehouse, START, END)
    same = reconciliation_gate.check_baseline(baseline["platform"], baseline)
    assert same[0]["status"] == "PASS"

    shifted = copy.deepcopy(baseline["platform"])
    shifted["settlement_rate"] = 103.7
    drift = reconciliation_gate.check_baseline(shifted, baseline)
    assert drift[0]["status"] == "FAIL"
    assert "settlement_rate 55.56 -> 103.7" in drift[0]["detail"]


def test_smoke_test_passes_against_a_healthy_api(warehouse, authed):
    expected = reconciliation_gate.expected_kpis(warehouse, START, END)["platform"]
    report = smoke_test.run_smoke(authed, START, END, expected=expected)
    assert report["passed"], [r for r in report["results"] if r["status"] == "FAIL"]


def test_smoke_test_fails_on_wrong_kpi_even_though_health_is_green(warehouse, authed):
    expected = reconciliation_gate.expected_kpis(warehouse, START, END)["platform"]
    report = smoke_test.run_smoke(InflatingClient(authed, 1.9), START, END, expected=expected)
    status = {r["check"]: r["status"] for r in report["results"]}
    assert status["health"] == "PASS"
    assert status["settlement_rate_within_0_100"] == "FAIL"
    assert status["settled_not_above_transacted"] == "FAIL"
    assert status["kpi_values_match_expected"] == "FAIL"
    assert not report["passed"]
