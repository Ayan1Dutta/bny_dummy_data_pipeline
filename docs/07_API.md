# Task 3 Parts A and B — API

> Deliverable for: *"Create GET /api/v1/settlement-summary and GET /api/v1/merchant-exceptions using FastAPI, Pydantic, OpenAPI."*

Implementation: `src/api/main.py` (routing, validation), `src/api/schemas.py` (Pydantic contracts), `src/api/repository.py` (parameterised SQL), `src/api/security.py` (auth, rate limiting).

---

## 1. Endpoint catalogue

| Method | Path | Purpose | Auth |
|---|---|---|---|
| GET | `/health` | liveness for the load balancer | none |
| GET | `/ready` | readiness: warehouse reachable, gold populated, last batch status | none |
| GET | `/api/v1/settlement-summary` | KPIs 1–4 for a window, optionally one merchant | API key |
| GET | `/api/v1/merchant-exceptions` | KPI 5: merchants breaching thresholds | API key |
| GET | `/api/v1/daily-trend` | daily transaction vs settled series (dashboard chart 1) | API key |
| GET | `/api/v1/top-merchant-gaps` | largest settlement gaps (dashboard chart 2) | API key |
| GET | `/api/v1/data-quality` | rule-level data quality summary | API key |
| GET | `/openapi.json`, `/docs` | machine and human contract | none |

## 2. Part A — settlement summary

```
GET /api/v1/settlement-summary?start_date=2026-09-01&end_date=2026-09-07
X-API-Key: <key>
```
```json
{
  "start_date": "2026-09-01",
  "end_date": "2026-09-07",
  "merchant_id": null,
  "transaction_count": 4842,
  "transaction_amount": 432558048.71,
  "settled_amount": 399686115.82,
  "settlement_rate": 92.4,
  "settlement_gap": 32871932.89,
  "sla_rate": 91.22,
  "unsettled_count": 170,
  "generated_at": "2026-09-24T05:39:01.803468"
}
```

The six fields the brief specifies are all present. Four additions are deliberate:
- `start_date` / `end_date` / `merchant_id` echo the request, so a cached or forwarded response is self-describing.
- `unsettled_count` turns "there is a gap" into "here is how many transactions to work".
- `generated_at` supports staleness checks on the dashboard.

`transaction_count` is the count of **successful** transactions, matching KPI 1's value definition. Attempt counts are available at aggregate grain (`transaction_count` vs `success_count`) for funnel analysis.

Reading path: the endpoint sums `gold.agg_daily_merchant_settlement`, which is already at date × merchant grain. SLA is recomputed from stored numerator and denominator (`sla_met_count` / `sla_eligible_count`) rather than averaging daily rates — averaging rates would weight a quiet day the same as a heavy one.

## 3. Part B — merchant exceptions

```
GET /api/v1/merchant-exceptions?start_date=2026-09-01&end_date=2026-09-07
```
```json
[
  {
    "merchant_id": "M1009",
    "merchant_name": "Global Stores 9",
    "settlement_rate": 69.8,
    "sla_rate": 63.77,
    "risk_level": "MEDIUM",
    "settlement_gap": 5440093.63,
    "transaction_amount": 18011676.64,
    "unsettled_count": 17,
    "breach_reasons": ["settlement_rate<95.0", "sla_rate<90.0"]
  }
]
```

- Filter is `settlement_rate < 95 OR sla_rate < 90`, as Part B specifies; both thresholds are query parameters (`settlement_rate_threshold`, `sla_rate_threshold`) so operations can tighten them without a deployment.
- Ordered by `settlement_gap DESC` — the list is a work queue, so it is sorted by money at stake rather than alphabetically.
- `risk_level` is the level effective on the **last transaction date in the window** (`arg_max(risk_level, transaction_date)`), never today's level.
- `breach_reasons` states why each merchant is listed, so the UI does not have to re-derive the rule.
- The `UNKNOWN` merchant member (`merchant_key = -1`) is excluded here — it is a data quality problem, not a merchant, and it is reported through `/api/v1/data-quality` instead.

## 4. Validation and status codes

| Code | When | Mechanism |
|---|---|---|
| 200 | valid request | — |
| 400 | `start_date` or `end_date` is not an ISO date, start after end, or range wider than 366 days | explicit parse in `parse_window()` |
| 401 | missing or wrong API key | `verify_api_key` dependency, constant-time compare |
| 404 | `merchant_id` is well-formed but does not exist | existence check against `gold.dim_merchant` |
| 422 | parameter fails schema validation: `limit=0`, `merchant_id` failing `^[A-Za-z0-9_-]{1,32}$`, missing required parameter | Pydantic / FastAPI `Query` constraints |
| 429 | more than 120 requests per minute per identity | sliding window in `security.rate_limit` |
| 500 | unexpected error | global handler returns a generic message; the detail goes to the log with a request id |

Dates are parsed explicitly rather than typed as `date` in the signature, precisely so a malformed date returns **400** (a bad value) while a structurally invalid parameter returns **422** (a bad request shape). That distinction is what the brief asks for, and FastAPI's default would collapse both into 422.

All five codes are asserted in `tests/test_api_contract.py`.

## 5. Contract and typing

Pydantic v2 response models carry the invariants, so they are enforced at the boundary rather than trusted:

```python
class SettlementSummary(BaseModel):
    transaction_count: int = Field(ge=0)
    transaction_amount: float = Field(ge=0)
    settled_amount: float = Field(ge=0)
    settlement_rate: float = Field(ge=0, le=100)
    sla_rate: float = Field(ge=0, le=100)
```

A pipeline bug that produced a 104% settlement rate would fail serialization instead of reaching the dashboard. The same bound exists as a CHECK constraint in the warehouse and as a test assertion — three independent layers, because a rate above 100 means money was invented.

OpenAPI is published at `/openapi.json` and asserted by `test_openapi_contract_is_published`, so a contract change cannot ship silently.

## 6. Security posture (summary; detail in `10_SECURITY.md`)

- Every query is parameterised — no user value is ever concatenated into SQL.
- `merchant_id` is additionally constrained by regex, so injection payloads fail at 422 before reaching the data layer.
- API key via `X-API-Key`, compared with `hmac.compare_digest`; production swaps this for OIDC/JWT at the gateway.
- CORS is an allow-list from configuration, GET only, no credentials.
- Responses carry `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `X-Request-ID`.
- The warehouse role used by the API holds SELECT on three Gold objects and nothing else.
- No endpoint returns a customer identifier; the Gold layer does not contain one.

## 7. Performance

- The serving aggregate means a 7 day platform query touches 276 rows, not 6,900 transactions; p95 target is under 300 ms.
- A single read-only warehouse connection is reused under a lock; in PostgreSQL this becomes a small connection pool.
- Date range is capped at 366 days so a client cannot request an unbounded scan.
- `limit` is capped per endpoint (50–500) to bound response size.

## 8. Running and calling it

```bash
make api                     # uvicorn src.api.main:app --reload --port 8000
open http://127.0.0.1:8000/docs

curl -H "X-API-Key: dev-local-key-change-me" \
  "http://127.0.0.1:8000/api/v1/settlement-summary?start_date=2026-09-01&end_date=2026-09-07"

curl -H "X-API-Key: dev-local-key-change-me" \
  "http://127.0.0.1:8000/api/v1/merchant-exceptions?start_date=2026-09-01&end_date=2026-09-07&limit=10"
```

## 9. What a v2 would add

Cursor pagination on the exception list, ETag/`If-None-Match` support (the aggregate changes only when a batch lands), an `as_of_batch` parameter for reproducing a report exactly as it looked at a point in time, and a webhook that pushes new exceptions into the operations queue instead of waiting for a poll.
