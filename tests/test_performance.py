from __future__ import annotations

import statistics
import time

import pytest
from fastapi.testclient import TestClient

from src.config import settings

WINDOW = {"start_date": "2026-09-01", "end_date": "2026-09-07"}
BUDGET_MS = 500.0


@pytest.fixture()
def authed(client):
    from src.api.main import app

    with TestClient(app, headers={"X-API-Key": settings.api_key}) as c:
        yield c


def p95(client, path, params, samples):
    timings = []
    for _ in range(samples):
        started = time.perf_counter()
        response = client.get(path, params=params)
        timings.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200
    return statistics.quantiles(timings, n=20)[-1]


@pytest.mark.parametrize(
    "path,params",
    [
        ("/api/v1/settlement-summary", WINDOW),
        ("/api/v1/settlement-summary", {**WINDOW, "merchant_id": "M100"}),
        ("/api/v1/merchant-exceptions", WINDOW),
        ("/api/v1/daily-trend", WINDOW),
        ("/health", {}),
    ],
)
def test_p95_latency_is_under_500_ms(authed, path, params):
    assert p95(authed, path, params, samples=30) < BUDGET_MS
