# Task 2 (Part 1) — Data Model

> Deliverable for: *"Build the physical model. The engineer must justify the model. The grain of every fact table must be explicitly documented."*

---

## 1. Model at a glance

```
              dim_date                    dim_merchant (SCD2)          dim_customer
                 |                              |                          |
                 |  date_key                    | merchant_key             | customer_key
                 |                              |                          |
     +-----------+------------------------------+--------------------------+
     |                          |                           |
fact_transaction        fact_settlement            fact_payment_event
  (1 per attempt)      (1 per settlement)           (1 per event)
     |                          |
     +------------+-------------+
                  |
   fact_settlement_reconciliation      <-- 1 row per SUCCESSFUL transaction
                  |                        settlements already collapsed here
                  |
   agg_daily_merchant_settlement        <-- 1 row per date x merchant, serves the API
```

## 2. Grain — declared explicitly for every table

| Table | Grain (one row =) | Primary key | Why this grain |
|---|---|---|---|
| `gold.dim_date` | one calendar day | `date_key` | conformed date for every fact |
| `gold.dim_merchant` | one merchant **risk validity period** | `merchant_key` (surrogate), natural `(merchant_id, effective_from)` | risk is historical; a merchant with three risk periods is three rows |
| `gold.dim_customer` | one customer, pseudonymised | `customer_key` | keeps customer analysis possible without holding the identifier |
| `gold.fact_transaction` | one payment transaction **attempt** | `transaction_id` | the source grain; keeps FAILED and REVERSED visible for funnel analysis |
| `gold.fact_settlement` | one settlement **record** | `settlement_id` | the source grain; a transaction legitimately has many |
| `gold.fact_payment_event` | one payment **lifecycle event** | `event_id` | supports latency and ordering analysis |
| `gold.fact_settlement_reconciliation` | one **successful transaction**, settlements pre-aggregated | `transaction_id` | the fan-out guard and the place every money KPI is derived |
| `gold.agg_daily_merchant_settlement` | one `transaction_date` × `merchant_key` | composite | the serving grain the API reads |

Silver mirrors the source grain exactly (`silver.transactions` one row per attempt, `silver.settlements` one row per settlement, `silver.merchant` one row per validity period, `silver.payment_events` one row per event). Bronze grain is "one row as physically received", including duplicates.

## 3. Why this model, and what was rejected

**Why a star schema rather than one wide table.** Operations filters by date, merchant and risk, and totals money. That is exactly what conformed dimensions plus additive facts are for. A single wide table would recompute risk history on every query and make point-in-time correctness a per-analyst problem.

**Why `fact_settlement_reconciliation` exists at all.** This is the most important design decision in the build, and the interviewer will ask about it.

A transaction can have many settlements. Joining `fact_transaction` to `fact_settlement` directly and summing `amount` multiplies every split transaction by its number of settlement rows:

```sql
-- WRONG: fan-out. Sample dataset returns 57.49 Cr.
SELECT sum(t.amount)
FROM gold.fact_transaction t
JOIN gold.fact_settlement s ON s.transaction_id = t.transaction_id
WHERE t.is_successful;

-- RIGHT: aggregate to transaction grain first. Sample dataset returns 55.91 Cr.
SELECT sum(t.amount), sum(coalesce(s.settled_amount, 0))
FROM gold.fact_transaction t
LEFT JOIN (
    SELECT transaction_id, sum(settlement_amount) AS settled_amount
    FROM gold.fact_settlement
    WHERE settlement_status = 'SETTLED' AND NOT is_orphan
    GROUP BY transaction_id
) s ON s.transaction_id = t.transaction_id
WHERE t.is_successful;
```

The difference is ₹1.59 Cr of imaginary money on one week of sample data. Rather than trust every future analyst to remember the sub-query, the collapse is done **once**, in the pipeline, into a governed table at transaction grain. Every consumer — API, dashboard, ad-hoc SQL — reads the safe table.

**Why a daily merchant aggregate on top.** The API must answer a 7 day KPI request in milliseconds. At date × merchant grain the sample week is 276 rows instead of 6,900 transactions, and the aggregate carries pre-computed SLA numerators and denominators so rates are summed, never averaged. (Averaging daily rates is a classic bug: it weights a ₹100 day the same as a ₹1 Cr day.)

**Why SCD Type 2 on merchant.** The source supplies `effective_from` / `effective_to`, so risk history already exists. Type 1 would overwrite it and make February transactions look like they were always MEDIUM risk — which would be wrong for regulatory review. The `merchant_key` surrogate resolves by date, so the fact row is permanently bound to the risk that actually applied.

**Why a customer dimension at all.** Two reasons: the grain question "one row per transaction, not per customer" becomes checkable; and it gives a place to pseudonymise. `customer_id` never leaves Silver — Gold holds `md5(salt || customer_id)`.

**What was rejected**
- Bridge table between transaction and settlement: the relationship is a simple 1:N, not M:N. A pre-aggregated fact is cheaper and safer.
- Snapshot fact of daily settlement balances: attractive for finance, but the brief asks for gap attribution, which needs transaction grain.
- Merging settlement columns into `fact_transaction`: destroys the settlement grain and makes batch-level analysis impossible.

## 4. Keys, constraints and referential integrity

| Constraint type | Applied where |
|---|---|
| Primary keys | every Silver and Gold table (see the grain table above) |
| Foreign keys | `fact_transaction` → `dim_date`, `dim_merchant`, `dim_customer`; `fact_settlement` / `fact_payment_event` → `dim_date`; `fact_settlement_reconciliation` → `dim_date`, `dim_merchant` |
| CHECK constraints | `amount >= 0`, `currency IN ('INR')`, `status IN (...)`, `settlement_status IN (...)`, `risk_level IN (...)`, `settlement_state IN (...)`, `settlement_rate BETWEEN 0 AND 100.001` |
| Uniqueness | `(merchant_id, effective_from)` on the merchant dimension; `(transaction_date, merchant_key)` on the aggregate |
| Non-overlap | PostgreSQL `EXCLUDE USING gist (merchant_id WITH =, daterange(effective_from, effective_to,'[]') WITH &&)` — the database physically refuses overlapping risk windows |

Referential integrity is enforced **and** made survivable:
- An unknown `merchant_id` does not break the load; it resolves to `merchant_key = -1` ("Unknown merchant") so platform totals still reconcile, and rule TXN-010 counts it.
- A settlement whose transaction is missing is loaded with `is_orphan = true` and excluded from rates. The flag is re-evaluated every run, so a late-arriving parent silently repairs the relationship.

## 5. Indexing and partitioning

| Object | Physical design | Reason |
|---|---|---|
| `fact_transaction` | partition by `transaction_date` (monthly), index on `(merchant_key, transaction_date)` | every query is date-bounded; merchant drill-down is the second access path |
| `fact_settlement` | partition by `settlement_date`, index on `transaction_id` | the reconciliation join is by transaction |
| `fact_payment_event` | partition by `event_date` | highest volume table; old partitions detach cleanly |
| `agg_daily_merchant_settlement` | PK `(transaction_date, merchant_key)`, index on `(merchant_id, transaction_date)` | serves both "whole platform for a week" and "one merchant for a week" |
| `dim_merchant` | index on `(merchant_id, effective_from, effective_to)` | supports the point-in-time band join |
| Bronze tables | partition by `_ingested_at` | archive raw partitions to Glacier without touching Silver |

## 6. Staging and layer contracts

| Layer | Contract | May a consumer read it? |
|---|---|---|
| Bronze | everything that arrived, as text, with lineage; no rules applied | pipeline and support only |
| Staging (`stg_txn`, `stg_stl`, `stg_evt`, `stg_merchant`) | in-flight temp tables holding typed values plus rule flags; the only place a record is both valid and invalid | no, transient |
| Silver | typed, deduplicated, constraint-clean, business-key unique | analysts with PII clearance |
| Gold | conformed dimensional model, no PII, governed measures | all reporting consumers |
| Serving aggregate | the only object the API reads for KPIs | API role, SELECT only |

## 7. Physical DDL

- Reference implementation (runs anywhere, no server): `sql/01_bronze.sql`, `sql/02_silver.sql`, `sql/03_gold.sql`, `sql/04_control.sql`
- Production PostgreSQL with partitions, sequences, exclusion constraint and role grants: `sql/90_postgres_production_ddl.sql`
- Grain, integrity and reconciliation checks as runnable SQL: `sql/91_reconciliation_and_dq_queries.sql`

## 8. Proving the model is right

```sql
-- Grain: expected 0 rows
SELECT transaction_id, count(*) FROM gold.fact_transaction
GROUP BY transaction_id HAVING count(*) > 1;

-- Referential integrity: expected 0 rows
SELECT f.transaction_id FROM gold.fact_transaction f
LEFT JOIN gold.dim_merchant m ON m.merchant_key = f.merchant_key
WHERE m.merchant_key IS NULL;

-- Reconciliation: settled value in the settlement fact must equal settled value in the reconciliation fact
SELECT (SELECT sum(settlement_amount) FROM gold.fact_settlement
        WHERE settlement_status = 'SETTLED' AND NOT is_orphan) AS from_settlements,
       (SELECT sum(settled_amount) FROM gold.fact_settlement_reconciliation) AS from_reconciliation;
```

All three run as automated tests in `tests/test_data_model.py` (13 tests), so the model is re-verified on every commit rather than on request.
