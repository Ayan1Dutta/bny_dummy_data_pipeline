# Runbook — running, demonstrating and operating the platform

---

## 1. First run, from a clean checkout

```bash
pip install -r requirements-dev.txt     # or: make install
python scripts/generate_sample_data.py  # or: make data
python -m src.pipeline.run_pipeline     # or: make pipeline
pytest                                  # 64 tests, ~3 seconds
```

Then, in two terminals:

```bash
make api      # http://127.0.0.1:8000  (docs at /docs)
make ui       # http://127.0.0.1:8080
```

Open `http://127.0.0.1:8080`, leave the window at 2026-09-01 → 2026-09-07, press **Load**. Default API key for local use is `dev-local-key-change-me`.

## 2. A 10 minute demonstration script

| Minute | Do this | Say this |
|---|---|---|
| 0–1 | Show the dashboard hero | "₹3.29 Cr of successful payments did not reach merchants this week. 92.4% of value settled, 91.2% within the 30 minute SLA." |
| 1–2 | Point at the KPI strip and the gap chart | "The gap is not one problem. Half of it is transactions with no settlement record at all, a quarter is pending, a fifth is failed settlements." |
| 2–3 | Scroll to the exception table | "Six merchants carry most of it. This list is sorted by money at stake and shows the risk level that applied during the window, not today's." |
| 3–4 | Show the data quality panel | "94 settlements arrived with no matching transaction, 26 transactions have no merchant, 330 events breached the ingestion SLA. Nothing was deleted — every one is queryable with its original payload." |
| 4–6 | Run `sql/91` queries 7 and 8 | "This is the trap in the data. A direct transaction-to-settlement join reports ₹57.49 Cr; the correct figure is ₹55.91 Cr. 481 transactions have more than one settlement. We collapse settlements once, in the pipeline, into a governed table." |
| 6–7 | `pytest -k "risk or late or idempotent" -v` | "Point-in-time risk, late arrival, idempotent reruns — all asserted, not asserted by me in a meeting." |
| 7–8 | Add a late settlement file, rerun the pipeline | "A settlement for the 1st arriving on the 3rd corrects the 1st, and recomputes exactly one partition." |
| 8–9 | `curl` a 400, a 404 and a 401 | "Validation is explicit: a malformed date is 400, an unknown merchant is 404, a bad parameter shape is 422, no key is 401." |
| 9–10 | Show `.gitlab-ci.yml` and the rollback job | "Tests, coverage, a data quality gate, SAST, dependency and secret scanning all block the build. Rollback is one click because images are immutable." |

## 3. Demonstrating incremental processing live

```bash
# 1. Baseline
python -m src.pipeline.run_pipeline | tail -20     # deltas are 0 on a second run

# 2. Drop a late settlement for a previously unsettled transaction
python - <<'PY'
from pathlib import Path
Path("data/raw/settlements_late.csv").write_text(
    "settlement_id,transaction_id,settlement_ts,settlement_amount,settlement_status,settlement_batch\n"
    "S999999,T100003,2026-09-09 09:00:00,5000.00,SETTLED,BLATE\n"
)
PY

# 3. Rerun and watch only the affected keys move
python -m src.pipeline.run_pipeline | tail -20
```

`affected_transactions` will be 1, and only that transaction's date × merchant aggregate row is rebuilt.

## 4. Useful operational queries

All in `sql/91_reconciliation_and_dq_queries.sql`. The four to know:

```sql
-- what moved in the last runs
SELECT * FROM ctl.pipeline_run ORDER BY started_at DESC LIMIT 10;

-- what needs a human
SELECT rule_id, severity, disposition, count(*) FROM ctl.dq_quarantine GROUP BY 1,2,3 ORDER BY 4 DESC;

-- how far behind are we
SELECT * FROM ctl.etl_watermark;

-- decompose today's gap
SELECT settlement_state, count(*), sum(settlement_gap)
FROM gold.fact_settlement_reconciliation
WHERE transaction_date = DATE '2026-09-01' GROUP BY 1 ORDER BY 3 DESC;
```

## 5. Common operational situations

| Situation | Diagnosis | Action |
|---|---|---|
| Settlement rate drops suddenly | decompose by `settlement_state`; check `ctl.dq_rule_result` for a spike | a PENDING spike is upstream processor delay; an UNSETTLED spike is a missing file |
| Dashboard shows stale numbers | `/ready` reports `last_batch_id` and status; check `ctl.etl_watermark` | rerun the pipeline; check the S3 trigger and the EventBridge rule |
| Quarantine volume jumps | group `ctl.dq_quarantine` by `rule_id` for the batch | if TXN-005 spikes, the merchant master is missing entries — fix at source and reload the file |
| A merchant disputes their settlement rate | `/api/v1/settlement-summary?merchant_id=...`, then the reconciliation table for that merchant | `settlement_state` per transaction gives the exact list to send them |
| Pipeline failed mid-run | `ctl.pipeline_run` holds the status and error; watermarks for incomplete stages are untouched | fix and rerun; the run is idempotent |
| A number looks wrong after a code change | compare against the fixture expectations in `tests/conftest.py` | `--full-refresh` rebuilds Gold from the facts |

## 6. Rebuild and recovery

```bash
python -m src.pipeline.run_pipeline --full-refresh   # rebuild reconciliation + aggregate from facts
rm data/warehouse/settlement.duckdb && make pipeline  # rebuild everything from raw files
```

Bronze is immutable and file-audited, so a full rebuild never requires asking source systems for data again.

## 7. Repository map

```
docs/          one markdown answer per sprint question (start with 00_INTERVIEW_MASTER_GUIDE.md)
features/      25 Gherkin acceptance scenarios
sql/           01-04 reference DDL, 90 production PostgreSQL DDL, 91 reconciliation + DQ query pack
src/
  config.py            environment-driven settings, no secrets
  db.py                connection handling, parameterised execution
  quality/rules.py     28 data quality rules with severity and disposition
  ingestion/bronze.py  file discovery, hashing, landing, audit
  transform/silver.py  typing, dedup, validation, quarantine routing
  transform/gold.py    dimensions, facts, reconciliation, daily aggregate
  pipeline/            orchestrator CLI with run logging
  api/                 FastAPI app, Pydantic schemas, repository, security
frontend/index.html    dashboard, no build step, no external dependencies
tests/                 64 tests across transformation, model, contract, security
scripts/               sample data generator, DQ gate, smoke test, ECS task renderer
infra/terraform/       AWS infrastructure as code
infra/aws/             Lambda trigger, Glue PySpark job, Step Functions definition
.gitlab-ci.yml         eight-stage pipeline with gates and rollback
```
