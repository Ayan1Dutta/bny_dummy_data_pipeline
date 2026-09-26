# Task 4 Part D — Security

> Deliverable for: *"Identify and prevent SQL injection, unrestricted API access, secrets in source code, PII leakage, excessive logging, missing authorization."*

---

## 1. SQL injection

**The vulnerable pattern the brief shows, and why it is fatal here**

```python
query = f"""
SELECT * FROM transactions
WHERE merchant_id = '{merchant_id}'
"""
```
`merchant_id = "' OR '1'='1"` returns every merchant's transactions — a cross-merchant data breach in a system holding settlement obligations. `"'; DROP TABLE transactions; --"` is worse.

**What this build does instead**

```python
def settlement_summary(start: date, end: date, merchant_id: str | None) -> dict:
    sql = """
        SELECT ... FROM gold.agg_daily_merchant_settlement
        WHERE transaction_date BETWEEN ? AND ?
          AND (? IS NULL OR merchant_id = ?)
    """
    row = _fetch(sql, [start, end, merchant_id, merchant_id])
```

Four defences, layered:

1. **Parameter binding everywhere.** Values travel as bound parameters, never as string content. The optional merchant filter uses the `(? IS NULL OR col = ?)` idiom so even the *shape* of the query does not change with user input.
2. **Input allow-listing.** `merchant_id` must match `^[A-Za-z0-9_-]{1,32}$`, enforced by FastAPI, so `M100' OR '1'='1` is rejected with 422 before it reaches the data layer. Dates are parsed with `date.fromisoformat`, so a date can only ever be a date.
3. **Least privilege.** The API's database role has SELECT on three Gold objects. Even a successful injection could not write, drop, or read Silver where the customer identifiers live.
4. **A test that enforces the rule permanently.** `test_sql_is_not_built_by_string_formatting_of_user_input` scans every SQL string in `src/` and fails the build if a Python expression is interpolated into one outside a small allow-list of internal identifiers (table names, generated predicate fragments — never user input). Four live injection payloads are also fired at the API in `test_injection_payloads_are_rejected_and_harmless`, which then asserts the warehouse is intact.

The pipeline's internal SQL does compose rule predicates into query text — but those predicates come from `src/quality/rules.py`, a source-controlled catalogue, never from a request. That distinction is exactly what the test encodes.

## 2. Unrestricted API access

| Control | Implementation | Production form |
|---|---|---|
| Authentication | `X-API-Key` header compared with `hmac.compare_digest` (constant time, so the key cannot be guessed by timing) | OIDC / JWT from the corporate IdP, validated at the gateway |
| Rate limiting | 120 requests per minute per identity, sliding window | AWS WAF + API Gateway throttling, per-consumer quotas |
| Network exposure | CORS allow-list from configuration, GET only, no credentials | internal-only ALB, TLS 1.3, ingress restricted to corporate CIDRs |
| Resource bounds | date range capped at 366 days, `limit` capped per endpoint | unchanged |
| Health endpoints | `/health` and `/ready` are unauthenticated but return no business data | unchanged |

`test_missing_api_key_returns_401` and `test_wrong_api_key_returns_401` keep the door shut.

## 3. Secrets in source code

- Every secret is read from the environment: `API_KEY`, `PII_SALT`, and in production the warehouse credentials. `src/config.py` contains no literal secret; the development defaults are obvious placeholders (`dev-local-key-change-me`).
- In AWS, values come from Secrets Manager and are injected as ECS task `secrets` (see `scripts/render_task_def.py`), so they never appear in the task definition, the image, or a log line.
- `.env` is git-ignored; `.env.example` documents the variables with placeholder values.
- CI runs `detect-secrets` across all files and fails on any finding; `test_no_credentials_are_hardcoded_in_source` additionally scans for AWS key patterns, hardcoded passwords and bearer tokens.
- Secrets rotate through Secrets Manager rotation; the application reads them at start-up, so rotation is a rolling restart.

## 4. PII leakage

The feeds contain `customer_id`, which is personal data in a payments context.

| Layer | Customer identifier | Reason |
|---|---|---|
| Bronze | present, as received | immutable audit record, restricted access, encrypted |
| Silver | present, plus `customer_key_hash` = `md5(salt + customer_id)` | analysts with clearance can still join |
| Gold | **hash only**, in `dim_customer.customer_id_hash` | analysis remains possible, identification does not |
| API | absent entirely | no endpoint has a field for it |
| Dashboard | absent entirely | nothing to leak |

The salt lives in Secrets Manager, so the hash is not reversible by dictionary attack against a known identifier space. Two tests hold the line: `test_raw_customer_id_never_reaches_the_gold_layer` inspects `information_schema` for a `customer_id` column in Gold, and `test_responses_never_expose_customer_identifiers` scans every endpoint's response body for customer id patterns.

Data classification and retention: Bronze 90 days hot then Glacier, Silver 13 months, Gold 7 years (regulatory). Quarantine payloads inherit Bronze's classification because they contain raw records.

## 5. Excessive logging

- Logs carry method, path, status, duration and an `X-Request-ID` — no query parameters, no payloads, no SQL, no identifiers.
- Errors are logged server-side with a stack trace; the client receives `{"error": "internal_error", "detail": "An unexpected error occurred"}`. A leaked stack trace tells an attacker the ORM, the schema and the file layout.
- `test_error_responses_do_not_leak_internals` asserts that no response body contains "traceback", "duckdb" or "select".
- Log retention is 365 days in production, 30 in lower environments, encrypted with the application KMS key.
- Quarantine payloads are stored in the database rather than the log stream, so raw records sit under database access control instead of in a log aggregator.

## 6. Missing authorization

Authentication asks who you are; authorization asks what you may touch. Both exist:

- **Application layer:** every business endpoint carries `Depends(verify_api_key)`; only `/health`, `/ready` and the OpenAPI document are open.
- **Database layer:** `settlement_api_ro` has USAGE on `gold` and `ctl` and SELECT on exactly three objects — the aggregate, the merchant dimension and the date dimension — with `REVOKE ALL ON SCHEMA silver`. The ETL role is separate and is the only writer.
- **Infrastructure layer:** separate security groups for ALB, API and ETL; the database accepts connections only from the application tier; the load balancer is internal and restricted to corporate CIDRs; ECR images are immutable and scanned on push.
- **Role separation in production:** merchant support sees one merchant, operations sees all merchants, data engineering sees the control schema. With JWT this becomes a scope claim checked in the dependency — the injection point already exists.

## 7. Security controls summary

| Threat | Control | Verified by |
|---|---|---|
| SQL injection | bound parameters, regex allow-list, least privilege | `test_injection_payloads_are_rejected_and_harmless`, `test_sql_is_not_built_by_string_formatting_of_user_input` |
| Broken auth | constant-time key compare, 401 on failure | `test_missing_api_key_returns_401` |
| Excessive access | read-only role on three objects | DDL grants in `sql/90_postgres_production_ddl.sql` |
| Secrets exposure | environment + Secrets Manager, CI secret scan | `test_no_credentials_are_hardcoded_in_source`, `detect-secrets` job |
| PII exposure | hash at the Silver→Gold boundary | `test_raw_customer_id_never_reaches_the_gold_layer` |
| Information disclosure | generic errors, clean logs | `test_error_responses_do_not_leak_internals` |
| Clickjacking / sniffing | `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` | `test_security_headers_are_present` |
| Denial of service | rate limit, bounded ranges, capped limits | `security.rate_limit`, `parse_window` |
| Vulnerable dependencies | `pip-audit` gate in CI | `.gitlab-ci.yml` |
| Insecure code patterns | `bandit` SAST gate in CI | `.gitlab-ci.yml` |
| Data at rest | KMS on S3, RDS, ECR, CloudWatch Logs; rotation enabled | `infra/terraform/main.tf` |
| Data in transit | TLS 1.3, internal ALB | `infra/terraform/main.tf` |

## 8. What would be added for a real production release

A penetration test before go-live; mutual TLS between the dashboard's backend-for-frontend and the API; field-level encryption for quarantine payloads containing raw records; an access log fed into the SIEM with alerting on 401 spikes; and a formal data protection impact assessment covering the hash salt's lifecycle.
