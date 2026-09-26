# Task 2 (Part 2) — Data Pipeline

> Deliverable for: *"Raw → Validation → Clean → Business transformation → Gold. Invalid records should not silently disappear. The implementation should support incremental processing."*

---

## 1. Flow

```
data/raw/*.csv
   |  discover by pattern, SHA-256 file hash, skip if already loaded
   v
BRONZE            bronze.transactions / settlements / merchant / payment_events
   |              + _source_file, _source_row_num, _row_hash, _batch_id, _ingested_at
   |
   |  WHERE _ingested_at > watermark        <-- incremental read
   v
STAGING (temp)    TRY_CAST typing, trim/upper, dedup rank, rule flags
   |
   +--> ctl.dq_quarantine        REJECT + QUARANTINE rows, with original payload as JSON
   +--> ctl.dq_rule_result       per-rule failure counts for this batch
   |
   v
SILVER            typed, deduplicated, constraint-clean
   |
   |  WHERE _ingested_at > watermark
   v
GOLD facts        dim_date, dim_merchant (SCD2), dim_customer,
   |              fact_transaction (point-in-time merchant join), fact_settlement, fact_payment_event
   v
RECONCILIATION    collapse settlements to transaction grain for affected transaction ids only
   v
AGGREGATE         delete + insert affected (transaction_date, merchant_key) partitions only
   v
ctl.pipeline_run  batch id, mode, row counts, status
```

Run it: `python -m src.pipeline.run_pipeline` (incremental) or `--full-refresh` (rebuild the reconciliation and aggregate from all facts).

## 2. Bronze — land everything, judge nothing

`src/ingestion/bronze.py`

- Files are discovered by pattern, so `transactions.csv` and `transactions_20260909.csv` both load without configuration changes.
- Each file is SHA-256 hashed. If the same filename with the same hash was already loaded, the file is skipped — this is what makes replaying a directory safe.
- Every row is stamped with `_source_file`, `_source_row_num`, `_row_hash` (md5 of the concatenated source columns), `_batch_id` and `_ingested_at`.
- Everything is stored as text. No cast, no trim, no rule. If a value is garbage, Bronze keeps the garbage — that is the audit record.
- `ctl.load_file_audit` records file, batch, target, hash, row count and load time.

Why: Bronze is the replay surface. Any bug in Silver or Gold is fixed by changing code and reprocessing from Bronze, never by re-requesting files from five upstream systems.

## 3. Silver — type, deduplicate, validate, route

`src/transform/silver.py`

**Typing.** `TRY_CAST` rather than `CAST`: an unparsable timestamp becomes NULL and is routed by a rule, instead of aborting the batch. Money is `DECIMAL(18,2)`, never float.

**Deduplication** uses a window function with a deliberate ordering per entity:

| Entity | Partition by | Order by | Winner | Rule |
|---|---|---|---|---|
| transactions | `transaction_id` | `_ingested_at DESC` | latest version wins — a restatement is a correction | TXN-009 |
| settlements | `settlement_id` | `_ingested_at DESC` | latest version wins | STL-007 |
| payment_events | `event_id` | `ingestion_ts ASC` | **earliest** arrival wins — a re-delivered event is a duplicate, not a correction | EVT-004 |

**Validation** is declarative. `src/quality/rules.py` holds 28 rules, each with an id, severity, disposition, human-readable reason and a SQL predicate. The predicates are composed into the staging query, so adding a rule is a one-line change and the rule catalogue is the documentation.

**Routing.** Four dispositions:

| Disposition | Loaded to Silver | Written to `ctl.dq_quarantine` | Counted in `ctl.dq_rule_result` | Example |
|---|---|---|---|---|
| REJECT | no | yes | yes | blank id, unparsable timestamp, negative transaction amount |
| QUARANTINE | no | yes | yes | missing merchant, invalid currency, negative settlement |
| WARN | yes | no | yes | deduplicated row, late arriving event, unknown merchant |
| BUSINESS_EXCEPTION | yes | yes | yes | orphan settlement, over-settlement, ingestion SLA breach |

The quarantine row carries the full original record as JSON, so operations can see exactly what arrived, fix it at source, and the corrected file simply flows through on the next run.

**Nothing disappears.** The invariant is asserted by a test:
```
source rows = loaded rows + quarantined rows + deduplicated rows
6,988        = 6,923       + 65               + 0
```

**Orphan re-evaluation.** After loading settlements, the pipeline re-checks `is_orphan` for all settlements against the current transaction set. A settlement that arrived before its transaction stops being an orphan the moment the transaction lands — without a manual fix.

## 4. Gold — dimensions, facts, reconciliation, aggregate

`src/transform/gold.py`

**Dimensions.** `dim_date` generated from the data range; `dim_merchant` built from the SCD2 Silver rows plus an `UNKNOWN` member at `merchant_key = -1`; `dim_customer` built from salted hashes. Surrogate keys are deterministic hashes in the reference build and sequences in PostgreSQL, so re-running never renumbers a dimension.

**Point-in-time merchant join** — the whole answer to Issue 4:
```sql
LEFT JOIN gold.dim_merchant m
       ON m.merchant_id = t.merchant_id
      AND t.transaction_date BETWEEN m.effective_from AND m.effective_to
```
`effective_to` is normalised to `9999-12-31` at Silver, so the band join needs no NULL handling.

**Reconciliation** collapses settlements once per successful transaction and derives:

| Measure | Definition |
|---|---|
| `settled_amount` | sum of SETTLED settlement rows (non-orphan) |
| `pending_amount` / `failed_amount` | same for PENDING / FAILED |
| `settlement_count` | number of settlement rows — this is how a split is visible |
| `first_settled_ts` | min settlement timestamp among SETTLED rows |
| `minutes_to_settle` | `first_settled_ts - transaction_ts` in minutes |
| `is_sla_met` | `minutes_to_settle <= 30`, NULL when nothing settled |
| `settlement_gap` | `transaction_amount - settled_amount` |
| `settlement_state` | FULLY_SETTLED / PARTIALLY_SETTLED / PENDING / SETTLEMENT_FAILED / UNSETTLED |

**Aggregate** is rebuilt only for affected `(transaction_date, merchant_key)` pairs, and stores SLA numerator and denominator separately (`sla_met_count`, `sla_eligible_count`) so any window is summed correctly rather than averaged.

## 5. Incremental processing

Three mechanisms, each answering a different failure mode:

| Mechanism | Stops | Implementation |
|---|---|---|
| File hash audit | reloading the same file | `ctl.load_file_audit` (file + hash) |
| Watermark per target table | reprocessing old rows | `ctl.etl_watermark.last_ingested_at`, used as `WHERE _ingested_at > ?` |
| Affected-key recompute | reprocessing whole history when late data arrives | `tmp_affected_txn` → `tmp_affected_grain` → scoped DELETE + INSERT |

**The affected-key idea, in one paragraph.** A naive incremental pipeline recomputes "today". That is wrong here, because a settlement for the 1st can arrive on the 3rd. Instead the pipeline collects the transaction ids touched by this batch (new transactions **plus** the parents of new settlements), recomputes reconciliation for exactly those, derives the distinct `(transaction_date, merchant_key)` pairs they belong to, and rebuilds only those aggregate partitions. Late data therefore corrects the day it actually belongs to, and the work stays proportional to the delta, not to history.

Proven by test:
```
test_pipeline_rerun_is_idempotent            rerun with no new files -> 0 delta rows, identical snapshot
test_incremental_run_only_processes_new_records  1 new transaction -> 1 affected transaction
test_late_arriving_settlement_reopens_a_closed_day
        settlement for 01 Sep arriving on 03 Sep -> T5 becomes FULLY_SETTLED,
        01 Sep settled amount rises from 20,000 to 23,000, only that partition rebuilt
```

## 6. Idempotency and failure handling

- Every write is `INSERT OR REPLACE` on a declared key, or a scoped `DELETE` + `INSERT`. There is no `INSERT` that can double-count.
- `ctl.pipeline_run` opens a row with status RUNNING and closes it SUCCESS or FAILED with the error message, giving support a run history without reading logs.
- A failed batch leaves watermarks untouched for the stages that did not complete, so the next run picks up the same delta.
- Backfill: `--full-refresh` rebuilds reconciliation and aggregate from the facts. Full rebuild from source is `rm data/warehouse/settlement.duckdb && make pipeline` — legitimate because Bronze is reproducible from the immutable landing zone.

## 7. Observed behaviour on the shipped sample

| Stage | Rows |
|---|---|
| Bronze transactions / settlements / events / merchant | 6,988 / 6,622 / 20,325 / 49 |
| Silver loaded | 6,923 / 6,597 / 20,301 / 49 |
| Quarantined | 65 transactions, 25 settlements |
| Business exceptions | 94 orphan settlements, 330 ingestion SLA breaches |
| Warnings | 390 late events, 24 duplicate events, 41 unknown merchants |
| Reconciliation rows | 6,271 successful transactions |
| Aggregate rows | 276 date × merchant |
| Second run, no new files | 0 rows processed at every stage |

## 8. Scaling this design

The layering, grain and incremental strategy do not change with volume; only the engine does.

| Volume | Engine | What changes |
|---|---|---|
| Millions of rows/day (now) | DuckDB or PostgreSQL, single container | nothing |
| Tens of millions/day | PostgreSQL partitioned, or Glue + Delta on S3 | `infra/aws/glue_job_bronze_to_silver.py` is the same Silver logic in PySpark |
| Near real time | Kinesis → Flink/Spark Structured Streaming | Bronze becomes a stream sink; Silver rules and Gold model are unchanged because they already separate business time from processing time |
