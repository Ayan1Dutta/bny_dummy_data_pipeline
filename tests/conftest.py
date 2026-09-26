from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import db
from src.api import repository
from src.config import settings
from src.pipeline import run_pipeline

MERCHANTS = """merchant_id,merchant_name,merchant_category,country,risk_level,effective_from,effective_to
M100,Alpha Retail,GROCERY,IN,LOW,2026-01-01,2026-03-31
M100,Alpha Retail,GROCERY,IN,HIGH,2026-04-01,2026-06-30
M100,Alpha Retail,GROCERY,IN,MEDIUM,2026-07-01,
M200,Beta Traders,FUEL,IN,LOW,2026-01-01,
"""

TRANSACTIONS = """transaction_id,merchant_id,customer_id,transaction_ts,amount,currency,status,payment_channel
T1,M100,C1,2026-02-10 10:00:00,1000.00,INR,SUCCESS,POS
T2,M100,C1,2026-09-01 10:00:00,10000.00,INR,SUCCESS,ONLINE
T3,M200,C2,2026-09-01 11:00:00,5000.00,INR,SUCCESS,QR
T4,M200,C2,2026-09-01 12:00:00,2000.00,INR,FAILED,POS
T5,M200,C3,2026-09-01 13:00:00,3000.00,INR,SUCCESS,POS
T6,,C4,2026-09-01 14:00:00,900.00,INR,SUCCESS,POS
T7,M200,C5,2026-09-01 15:00:00,-50.00,INR,SUCCESS,POS
T8,M999,C6,2026-09-01 16:00:00,4000.00,INR,SUCCESS,ONLINE
T9,M200,C7,2026-09-01 17:00:00,7000.00,USD,SUCCESS,ONLINE
T10,M100,C8,2026-09-01 18:00:00,6000.00,INR,SUCCESS,POS
T11,M100,C9,2026-09-01 19:00:00,8000.00,INR,SUCCESS,QR
T2,M100,C1,2026-09-01 10:00:00,10000.00,INR,SUCCESS,QR
"""

SETTLEMENTS = """settlement_id,transaction_id,settlement_ts,settlement_amount,settlement_status,settlement_batch
S1,T1,2026-02-10 10:10:00,1000.00,SETTLED,B1
S2,T2,2026-09-01 10:05:00,8000.00,SETTLED,B2
S3,T2,2026-09-01 10:20:00,2000.00,SETTLED,B2
S4,T3,2026-09-01 12:00:00,5000.00,SETTLED,B3
S5,T99,2026-09-01 12:10:00,999.00,SETTLED,B3
S6,T10,2026-09-01 18:30:00,6000.00,PENDING,B4
S7,T11,2026-09-01 19:10:00,5000.00,SETTLED,B4
S8,T3,2026-09-01 12:05:00,-500.00,SETTLED,B3
"""

EVENTS = """event_id,transaction_id,event_type,event_ts,ingestion_ts,processing_ms
E1,T2,CREATED,2026-09-01 10:00:00,2026-09-01 10:00:05,120
E2,T2,AUTHORIZED,2026-09-01 10:00:10,2026-09-01 10:08:41,340
E3,T2,SETTLED,2026-09-01 10:05:00,2026-09-01 10:35:00,900
E4,T3,AUTHORIZED,2026-09-01 11:00:30,2026-09-01 11:01:00,150
E5,T3,CREATED,2026-09-01 11:00:00,2026-09-01 11:20:00,150
E1,T2,CREATED,2026-09-01 10:00:00,2026-09-01 10:09:00,120
"""

FILES = {
    "merchant.csv": MERCHANTS,
    "transactions.csv": TRANSACTIONS,
    "settlements.csv": SETTLEMENTS,
    "payment_events.csv": EVENTS,
}


@pytest.fixture(scope="session")
def warehouse(tmp_path_factory) -> str:
    base = tmp_path_factory.mktemp("bny")
    raw = base / "raw"
    raw.mkdir()
    for name, content in FILES.items():
        (raw / name).write_text(content, encoding="utf-8")
    target = str(base / "settlement.duckdb")
    run_pipeline.run(raw_dir=str(raw), warehouse=target)
    return target


@pytest.fixture(scope="session")
def raw_dir(warehouse) -> Path:
    return Path(warehouse).parent / "raw"


@pytest.fixture()
def con(warehouse):
    connection = db.connect(warehouse, read_only=True)
    yield connection
    connection.close()


@pytest.fixture(scope="session")
def client(warehouse):
    repository.set_warehouse(warehouse)
    with TestClient(app_instance()) as test_client:
        yield test_client
    repository.reset_connection()


def app_instance():
    from src.api.main import app

    return app


@pytest.fixture()
def auth_headers() -> dict:
    return {"X-API-Key": settings.api_key}
