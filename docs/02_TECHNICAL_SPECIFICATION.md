# Task 1.2 — Technical Specification

> Deliverable for: *"Document data sources, fields, processing, storage, API, front-end, validation, monitoring, security."*

---

## 1. Architecture

```
  transactions.csv   settlements.csv   merchant.csv   payment_events.csv
          |                 |               |                |
          +--------+--------+-------+-------+--------+-------+
                            |
                    INGESTION (file discovery, hash, audit)
                            |
                         BRONZE   raw text + lineage, nothing rejected
                            |
                         SILVER   typed, deduplicated, validated -> ctl.dq_quarantine
                            |
                          GOLD    dim_date / dim_merchant (SCD2) / dim_customer
                                  fact_transaction / fact_settlement / fact_payment_event
                                  fact_settlement_reconciliation  (fan-out guard)
                                  agg_daily_merchant_settlement   (serving)
                            |
                          API     FastAPI + Pydantic + OpenAPI, API key, rate limit
                            |
                      FRONT-END   static HTML/JS dashboard, API only
```

Control plane alongside every layer: `ctl.etl_watermark`, `ctl.load_file_audit`, `ctl.dq_quarantine`, `ctl.dq_rule_result`, `ctl.pipeline_run`.

## 2. Data sources and fields

| Source | Grain as delivered | Key | Volume in sample | Known problems |
|---|---|---|---|---|
| `transactions.csv` | one payment attempt | `transaction_id` | 6,988 rows | missing merchant_id, invalid currency, negative amount, duplicate ids |
| `settlements.csv` | one settlement record | `settlement_id` | 6,622 rows | 1:N to transaction, negative amounts, orphans |
| `merchant.csv` | one risk validity period | `merchant_id` + `effective_from` | 49 rows | open-ended `effective_to`, historical risk |
| `payment_events.csv` | one lifecycle event | `event_id` | 20,325 rows | duplicates, late arrival, out-of-order arrival |

Field-level typing, nullability and validation rule per column are defined in `src/quality/rules.py` and physically enforced by `sql/02_silver.sql` (CHECK constraints) and `sql/90_postgres_production_ddl.sql`.

| Field | Source type | Silver type | Rule |
|---|---|---|---|
| `transaction_id` | text | `VARCHAR PK` | TXN-001 reject if blank |
| `transaction_ts` | text | `TIMESTAMP NOT NULL` | TXN-002 reject if unparsable; drives `transaction_date` |
| `amount` | text | `DECIMAL(18,2) CHECK >= 0` | TXN-003 / TXN-004 |
| `currency` | text | `VARCHAR CHECK IN ('INR')` | TXN-006 quarantine |
| `status` | text | `VARCHAR CHECK IN (...)` | TXN-007 quarantine |
| `customer_id` | text | kept in Silver, hashed for Gold | never leaves Silver |
| `settlement_amount` | text | `DECIMAL(18,2)` | STL-003 / STL-004 |
| `settlement_status` | text | `VARCHAR CHECK IN (...)` | STL-005 |
| `effective_to` | text, nullable | `DATE NOT NULL` default `9999-12-31` | open window closed explicitly |
| `event_ts` / `ingestion_ts` | text | `TIMESTAMP NOT NULL` | EVT-002; lag drives EVT-005 / EVT-006 |

Decimal, never float, for money. Floats are used only for derived rates, which are bounded and rounded.

## 3. Processing

| Stage | Module | What it does | Incremental strategy |
|---|---|---|---|
| Ingestion | `src/ingestion/bronze.py` | discovers files by pattern, SHA-256 hashes each file, skips already-loaded identical files, stamps `_source_file`, `_source_row_num`, `_row_hash`, `_batch_id`, `_ingested_at` | file hash in `ctl.load_file_audit` |
| Silver | `src/transform/silver.py` | `TRY_CAST` typing, trimming, case normalisation, window-function dedup, rule evaluation, quarantine routing, orphan re-evaluation | `WHERE _ingested_at > watermark` |
| Gold facts | `src/transform/gold.py` | surrogate keys, point-in-time merchant join, fact upserts | `WHERE _ingested_at > watermark` per fact |
| Reconciliation | `src/transform/gold.py` | collapses settlements to transaction grain, derives state, gap, SLA | recompute only affected `transaction_id`s |
| Aggregate | `src/transform/gold.py` | daily merchant KPI table | delete + insert only affected `(transaction_date, merchant_key)` partitions |
| Orchestration | `src/pipeline/run_pipeline.py` | batch id, run log, failure capture | `--full-refresh` available for backfills |

Idempotency: every write is `INSERT OR REPLACE` on a declared key, or a scoped `DELETE` + `INSERT`. Rerunning is safe and proven by test.

## 4. Storage

| Layer | Reference implementation | Production |
|---|---|---|
| Raw files | local `data/raw` | S3 `settlement-{env}-landing/incoming/`, KMS encrypted, versioned, Glacier IR after 90 days |
| Bronze / Silver / Gold | DuckDB file (`data/warehouse/settlement.duckdb`) | RDS PostgreSQL 15 (`sql/90_postgres_production_ddl.sql`) or S3 + Glue/Delta at higher volume |
| Partitioning | indexes on date and merchant | declarative RANGE partitioning on `transaction_date` / `event_date`, monthly partitions |
| Retention | full history | Bronze 90 days hot then archive; Silver 13 months; Gold 7 years |

DuckDB is chosen for the reference build so the whole platform runs with `pip install` and no server, while keeping ANSI SQL, real constraints and columnar performance. The DDL is written to port to PostgreSQL without a model change; only physical design (sequences, partitions, GiST exclusion constraint) differs.

## 5. API

- FastAPI + Pydantic v2, OpenAPI published at `/openapi.json`, interactive docs at `/docs`.
- Endpoints: `/api/v1/settlement-summary`, `/api/v1/merchant-exceptions`, `/api/v1/daily-trend`, `/api/v1/top-merchant-gaps`, `/api/v1/data-quality`, plus `/health` and `/ready`.
- Every query is parameterised; the API role has SELECT only on three Gold objects.
- Versioned path prefix so a breaking change ships as `/api/v2` alongside v1.
- Full detail in `07_API.md`.

## 6. Front-end

Single static HTML file, no build step, no external CDN, charts drawn as inline SVG. Consumes the API only. Detail in `08_FRONTEND.md`.

## 7. Validation

Four dispositions, applied by rule, recorded per batch:

| Disposition | Meaning | Loaded to Silver? | Example |
|---|---|---|---|
| REJECT | structurally unusable | no | blank `transaction_id`, unparsable timestamp |
| QUARANTINE | real record, unusable until a human fixes it | no | missing merchant, invalid currency, negative settlement |
| WARN | usable, but the anomaly is counted | yes | duplicate id deduplicated, late arriving event |
| BUSINESS_EXCEPTION | valid data describing a business problem | yes | orphan settlement, over-settlement, ingestion SLA breach |

Rule results per batch land in `ctl.dq_rule_result`; the offending records land in `ctl.dq_quarantine` with the original payload as JSON. `scripts/dq_gate.py` fails the CI pipeline when critical failures appear or quarantine exceeds 2% of source rows.

## 8. Monitoring

| Signal | Source | Alert |
|---|---|---|
| Pipeline run status and duration | `ctl.pipeline_run` | CloudWatch alarm `settlement-{env}-pipeline-failed` |
| Quarantine volume and rule mix | `ctl.dq_rule_result` | DQ gate failure, SNS notification |
| Freshness / watermark lag | `ctl.etl_watermark` | alarm when the watermark is older than 2 hours during business hours |
| Platform settlement rate | `gold.agg_daily_merchant_settlement` | alarm below 95% for two consecutive hours |
| Late arrival profile | `gold.fact_payment_event` | dashboard panel and EVT-006 exception count |
| API health | `/health`, `/ready` | ALB target health, ECS circuit breaker |
| API errors and latency | structured logs with `X-Request-ID`, ALB metrics | alarm on 5xx > 5 in 5 minutes |

Logs are structured, carry a request id, and never contain customer identifiers or query text.

## 9. Security

| Control | Implementation |
|---|---|
| Injection | parameterised queries everywhere; identifiers validated by regex; enforced by `test_sql_is_not_built_by_string_formatting_of_user_input` |
| Authentication | `X-API-Key` compared with `hmac.compare_digest`; production uses OIDC/JWT via the corporate IdP |
| Authorisation | dedicated read-only DB role with SELECT on three Gold objects only |
| Rate limiting | per-identity sliding window in the app; WAF and API Gateway throttling in production |
| Secrets | environment variables sourced from AWS Secrets Manager; nothing in source; `detect-secrets` runs in CI |
| PII | `customer_id` never leaves Silver; Gold stores a salted hash; no endpoint returns customer data |
| Transport | TLS 1.3 at the ALB, internal-only load balancer, corporate CIDR ingress |
| Logging hygiene | no payloads, no parameters, no stack traces in responses |
| Encryption at rest | KMS on S3, RDS and ECR, key rotation enabled |
| Auditability | every record carries `_batch_id` and `_ingested_at`; every run is logged |

Full detail, including the vulnerable-versus-safe code comparison, in `10_SECURITY.md`.

## 10. Non-functional targets

| Attribute | Target | Basis |
|---|---|---|
| API p95 latency | < 300 ms for a 7 day window | serving aggregate is pre-computed at date × merchant grain |
| Pipeline runtime | < 2 minutes for a 15 minute incremental batch | sample run processes ~34,000 rows end to end in seconds |
| Freshness | 15 minutes | EventBridge schedule plus S3 event trigger |
| Recovery | replay from Bronze without touching source systems | Bronze is immutable and file-audited |
| Test coverage gate | 80% line coverage, 0 critical DQ failures | enforced in `.gitlab-ci.yml` |
