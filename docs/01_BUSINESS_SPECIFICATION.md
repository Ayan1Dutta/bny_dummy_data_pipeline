# Task 1.1 — Business Specification

> Deliverable for: *"Before writing implementation code, produce a mini specification."*
> Read this document top to bottom in an interview and you have answered the business half of Task 1.

---

## 1. Problem statement

Payments Operations at Bank of New York reconciles merchant settlements from a nightly Excel extract that stitches together five systems (payment gateway, transaction system, settlement processor, merchant risk system, finance system). The extract reports totals only.

On the reference day, ₹48 Cr of transaction volume produced ₹45.8 Cr of settlement. The ₹2.2 Cr difference cannot be explained, because the current report cannot separate the five things that all look identical in a total:

| Possible cause | What it actually is | Is it money at risk? |
|---|---|---|
| Failed payments | The payment never succeeded, so nothing is owed | No — it should never have been in the numerator |
| Pending settlements | Money is in flight, settlement record exists with status PENDING | Not yet — it is a timing difference |
| Processing delays | Settled, but outside the 30 minute SLA | No, but it is a service failure |
| Merchant risk controls | Settlement deliberately held for a HIGH risk merchant | No — it is an intended hold |
| Data quality problems | The platform could not process the record at all | Yes — this is invisible money |

Until those five are separated, operations cannot tell a merchant complaint from a pipeline bug, and the bank carries an unquantified settlement obligation.

## 2. Objective

Build an engineered platform — not a script — that, for any date range and any merchant:

1. states the successful transaction value, the settled value and the difference between them;
2. decomposes that difference into the five causes above with a record count and a rupee value for each;
3. identifies merchants whose settlement behaviour is abnormal, using the risk classification that applied **on the transaction date**;
4. exposes all of it through a versioned API that a dashboard and downstream systems consume;
5. processes new files incrementally, so the 07:00 report does not require reprocessing history;
6. proves its own correctness with an automated test suite.

## 3. Users

| User | What they do with it | Primary surface |
|---|---|---|
| Head of Payments | Asks "where is the gap and is it getting worse" | Dashboard KPI strip and daily trend |
| Settlement operations analyst | Works the exception queue merchant by merchant | Merchant exception table, `/merchant-exceptions` |
| Merchant support | Answers "where is my money" for a named merchant | `/settlement-summary?merchant_id=` |
| Data engineering / support | Watches pipeline health and the quarantine queue | `/ready`, `/api/v1/data-quality`, `ctl.pipeline_run` |
| Risk and compliance | Checks that risk classification was applied as at the transaction date | `gold.dim_merchant` SCD2 history |
| Finance | Reconciles platform totals to the general ledger | Query pack `sql/91_reconciliation_and_dq_queries.sql` |

## 4. KPI definitions

These are the contract. Every ambiguity is resolved explicitly, because an undefined KPI is the actual root cause of most "the dashboard is wrong" tickets.

### KPI 1 — Transaction volume
```
Transaction volume = SUM(amount) WHERE status = 'SUCCESS'
```
- FAILED and REVERSED attempts are excluded from the value; they are still counted as attempts (`transaction_count` vs `success_count`) so the funnel stays visible.
- Quarantined records are excluded from the value and reported separately — they are never silently added or dropped.
- Currency is INR only; a non-INR row is quarantined rather than converted, because there is no agreed FX rate source in scope.

### KPI 2 — Settlement rate
```
Settlement rate = 100 * SUM(settled_amount) / SUM(successful transaction amount)
```
- `settled_amount` counts settlement rows with `settlement_status = 'SETTLED'` only. PENDING and FAILED settlements contribute zero.
- Settlements are aggregated to transaction grain **before** the division. This is the single most important rule in the specification (see Issue 1 in `06_HIDDEN_DATA_PROBLEMS.md`).
- Orphan settlements (no matching transaction) are excluded from the numerator; including them would let unexplained money inflate the rate.
- The denominator is the transaction date window, not the settlement date window. A payment on 01 Sep settled on 03 Sep belongs to 01 Sep.

### KPI 3 — Settlement gap
```
Settlement gap = SUM(successful transaction amount) - SUM(settled amount)
```
Reported alongside its decomposition by settlement state: UNSETTLED, PENDING, SETTLEMENT_FAILED, PARTIALLY_SETTLED. In the shipped sample week the ₹3.29 Cr gap decomposes as 49.9% unsettled, 28.1% pending, 20.5% failed settlements, 1.5% partial — which is the answer the Head of Payments actually wanted.

### KPI 4 — Settlement SLA
```
SLA rate = 100 * COUNT(transactions where first SETTLED timestamp - transaction timestamp <= 30 minutes)
               / COUNT(transactions with at least one SETTLED settlement)
```
- Measured to the **first** settled record, so a split settlement is not penalised for its second leg.
- Transactions with no settled record are excluded from the denominator (they are already counted in the gap; counting them twice would double-punish and hide the SLA signal). This is stated as an assumption and is a deliberate, reversible choice.

### KPI 5 — Merchant risk
```
Merchant is at risk when settlement rate < 95% AND SLA rate < 90%
```
- The dashboard and API default to the stricter AND for the KPI 5 headline count; the exception **report** uses OR (as Task 3 Part B specifies) so operations sees a merchant failing either threshold. Both thresholds are request parameters, so operations can tighten them without a release.
- The risk level shown is the level effective on the last transaction date in the window, never today's level.

## 5. Scope

**In scope**
- Batch ingestion of the four CSV feeds, incremental by file and by watermark.
- Bronze → Silver → Gold layering with an explicit data quality disposition for every record.
- Point-in-time merchant risk (SCD Type 2).
- Settlement reconciliation at transaction grain, daily merchant aggregate for serving.
- REST API with OpenAPI contract, API key auth, rate limiting.
- Operations dashboard consuming the API only.
- Automated test suite, data quality gate, CI/CD design, AWS deployment design.

**Out of scope (and why)**
- Real-time streaming: the sources deliver files; the business need is a 15 minute refresh, not sub-second. The event model is already business-time based, so a Kinesis/Flink front end can replace ingestion without changing Silver or Gold.
- Multi-currency conversion: no agreed rate source; non-INR is quarantined and visible.
- Writing back to source systems: the platform reports, it does not remediate.
- Merchant-facing access: internal users only.
- Payouts, chargebacks, fees and tax: not in the supplied feeds.

## 6. Assumptions

| # | Assumption | Impact if wrong | How to change it |
|---|---|---|---|
| A1 | A transaction belongs to the date of `transaction_ts` in IST | Daily totals shift by up to a day | Single change in the date key derivation |
| A2 | Only `settlement_status = 'SETTLED'` is money received | Settlement rate changes materially | One CASE expression in the reconciliation build |
| A3 | Multiple settlements for one transaction are legitimate partial settlements, not duplicates | Would change dedup rules | Reconciliation already tracks `settlement_count` |
| A4 | A negative settlement is a data error, not a reversal | Reversals would be understated | Rule STL-004 disposition changes from QUARANTINE to a signed measure |
| A5 | A transaction with no `merchant_id` cannot be attributed and must be fixed at source | Those amounts sit outside merchant KPIs but stay in the quarantine report | Rule TXN-005 disposition |
| A6 | An unknown `merchant_id` still represents real money and is kept under an UNKNOWN dimension member | Totals stay reconciled at platform level | Rule TXN-010 |
| A7 | SLA is measured against the transaction timestamp, not the authorisation event | SLA rate changes slightly | One join in the reconciliation build |
| A8 | Events may arrive up to 24 hours late; the platform recomputes affected partitions | Very late data would need a wider recompute window | Affected-key logic already handles any lateness |

## 7. Business rules

| ID | Rule |
|---|---|
| BR-01 | Only SUCCESS transactions create a settlement obligation |
| BR-02 | Settlement value is the sum of SETTLED settlement rows for that transaction |
| BR-03 | A transaction is FULLY_SETTLED when settled value >= transaction amount (±₹0.01) |
| BR-04 | A transaction with settlements but zero settled value is PENDING or SETTLEMENT_FAILED by the status present |
| BR-05 | A transaction with no settlement row at all is UNSETTLED and is a settlement exception |
| BR-06 | Settled value greater than the transaction amount is an over-settlement business exception (STL-008) |
| BR-07 | Merchant risk applied to a transaction is the level effective on the transaction date |
| BR-08 | A settlement without a matching transaction is an orphan: loaded, flagged, excluded from rates, reported |
| BR-09 | Business time (`event_ts`, `transaction_ts`) drives reporting; processing time (`ingestion_ts`, `_ingested_at`) drives pipeline control only |
| BR-10 | No source record may be silently dropped: every record is loaded, quarantined, rejected or explicitly deduplicated, and the four counts must reconcile to the source count |
| BR-11 | Reported KPIs are derived from one governed aggregate, so the dashboard, the API and the analyst's SQL cannot disagree |
| BR-12 | Rerunning the pipeline on unchanged input must not change any published number |

## 8. Acceptance criteria

| # | Criterion | How it is proven |
|---|---|---|
| AC-01 | Settlement rate and SLA rate are always between 0 and 100 | `test_settlement_rate_is_bounded`, CHECK constraint on the aggregate |
| AC-02 | A transaction with two settlements is counted once, with both settlements summed | `test_one_to_many_settlement_is_aggregated_before_transaction_level_maths` |
| AC-03 | A February transaction uses February risk, a September transaction uses September risk | `test_merchant_risk_is_applied_as_of_the_transaction_date` |
| AC-04 | A successful transaction with no settlement appears as UNSETTLED in the exception report | `test_settlement_states_cover_pending_partial_and_unsettled` |
| AC-05 | Every rejected or quarantined record is retrievable with its reason and original payload | `test_missing_merchant_is_quarantined_not_dropped`, `ctl.dq_quarantine` |
| AC-06 | Source rows = loaded + quarantined + deduplicated | `test_source_rows_are_either_loaded_or_recorded_as_exceptions` |
| AC-07 | A rerun with no new files changes nothing | `test_pipeline_rerun_is_idempotent` |
| AC-08 | A settlement arriving two days late corrects the original transaction date | `test_late_arriving_settlement_reopens_a_closed_day` |
| AC-09 | The API returns 200 / 400 / 404 / 422 / 401 in the correct situations | `tests/test_api_contract.py` |
| AC-10 | No endpoint exposes a customer identifier | `test_responses_never_expose_customer_identifiers` |
| AC-11 | The dashboard reads only from the API, never from CSV | `frontend/index.html` has no file access; all data arrives via `fetch` |
| AC-12 | KPI figures are reproducible from a fixed fixture | `test_daily_aggregate_matches_hand_calculated_kpis` asserts ₹36,000 volume, ₹20,000 settled, 55.56% rate, 66.67% SLA |
