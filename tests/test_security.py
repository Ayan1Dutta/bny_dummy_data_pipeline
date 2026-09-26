from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
WINDOW = {"start_date": "2026-09-01", "end_date": "2026-09-07"}

INJECTION_PAYLOADS = [
    "M100' OR '1'='1",
    "M100'; DROP TABLE gold.fact_transaction; --",
    "' UNION SELECT customer_id FROM silver.transactions --",
    "M100%27",
]


def test_injection_payloads_are_rejected_and_harmless(client, auth_headers, con):
    for payload in INJECTION_PAYLOADS:
        res = client.get("/api/v1/settlement-summary", params={**WINDOW, "merchant_id": payload}, headers=auth_headers)
        assert res.status_code in (404, 422)
    assert con.execute("SELECT count(*) FROM gold.fact_transaction").fetchone()[0] > 0


def test_date_parameters_are_not_string_interpolated(client, auth_headers):
    res = client.get(
        "/api/v1/settlement-summary",
        params={"start_date": "2026-09-01' OR 1=1 --", "end_date": "2026-09-07"},
        headers=auth_headers,
    )
    assert res.status_code == 400


def test_responses_never_expose_customer_identifiers(client, auth_headers):
    for path, params in [
        ("/api/v1/settlement-summary", WINDOW),
        ("/api/v1/merchant-exceptions", WINDOW),
        ("/api/v1/daily-trend", WINDOW),
        ("/api/v1/top-merchant-gaps", WINDOW),
    ]:
        body = client.get(path, params=params, headers=auth_headers).text
        assert "customer_id" not in body
        assert not re.search(r'"C\d+"', body)


def test_raw_customer_id_never_reaches_the_gold_layer(con):
    columns = con.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'gold' AND lower(column_name) LIKE '%customer%'
        """
    ).fetchall()
    names = {c[0] for c in columns}
    assert "customer_id" not in names
    assert {"customer_key", "customer_id_hash"} & names


def test_error_responses_do_not_leak_internals(client, auth_headers):
    res = client.get("/api/v1/settlement-summary", params={"start_date": "oops", "end_date": "2026-09-07"}, headers=auth_headers)
    body = res.text.lower()
    assert "traceback" not in body
    assert "duckdb" not in body
    assert "select" not in body


def test_security_headers_are_present(client):
    res = client.get("/health")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["X-Request-ID"]


def test_no_credentials_are_hardcoded_in_source():
    patterns = [
        r"AKIA[0-9A-Z]{16}",
        r"aws_secret_access_key\s*=\s*['\"][^'\"]+['\"]",
        r"password\s*=\s*['\"](?!\s*os\.)[^'\"]{6,}['\"]",
        r"Bearer\s+[A-Za-z0-9\-_\.]{20,}",
    ]
    for file in SRC.rglob("*.py"):
        text = file.read_text(encoding="utf-8")
        for pattern in patterns:
            assert not re.search(pattern, text), f"{file.name} matched {pattern}"


def test_secrets_come_from_the_environment_not_literals():
    config = (SRC / "config.py").read_text(encoding="utf-8")
    for key in ("API_KEY", "PII_SALT", "WAREHOUSE_PATH"):
        assert f'os.getenv("{key}"' in config


def test_sql_is_not_built_by_string_formatting_of_user_input():
    for file in SRC.rglob("*.py"):
        text = file.read_text(encoding="utf-8")
        for match in re.finditer(r"f\"\"\"[^\"]*?(SELECT|INSERT|UPDATE|DELETE)[^\"]*?\"\"\"", text, re.IGNORECASE | re.DOTALL):
            block = match.group(0)
            placeholders = re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_\.]*)\}", block)
            allowed = {"select_cols", "hash_expr", "entity", "where", "case_rule", "case_reason", "severity_case",
                       "disposition_case", "key_expr", "stg", "table", "rule.predicate", "MERCHANT_KEY",
                       "CUSTOMER_KEY", "UNKNOWN_MERCHANT_KEY"}
            assert set(placeholders) <= allowed, f"{file.name} interpolates {set(placeholders) - allowed}"


def test_api_rejects_oversized_merchant_identifier(client, auth_headers):
    res = client.get("/api/v1/settlement-summary", params={**WINDOW, "merchant_id": "M" * 64}, headers=auth_headers)
    assert res.status_code == 422
