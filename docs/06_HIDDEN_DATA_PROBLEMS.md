# Section 3 — The Hidden Data Problems, and how each one is handled

> Deliverable for: *"The engineer is not told the following explicitly. They need to discover them during implementation/testing."*
> This is the document to talk from when the interviewer asks "what did you find in the data?"

---

## Issue 1 — One-to-many settlement (the money multiplier)

**What it looks like**
```
T1001  10,000.00
  +-- S5001  8,000.00  SETTLED
  +-- S5002  2,000.00  SETTLED
```
481 transactions in the sample dataset have more than one settlement row.

**Why it is dangerous.** A direct `fact_transaction JOIN fact_settlement` produces one row per settlement, so `sum(t.amount)` counts the transaction twice. On the sample week the naive join reports **₹57.49 Cr** of transaction value; the correct figure is **₹55.91 Cr**. That is ₹1.59 Cr of money that does not exist — and it silently *improves* the settlement rate, so nobody notices.

**How it is handled.** Settlements are aggregated to transaction grain once, in the pipeline, into `gold.fact_settlement_reconciliation`. Every money KPI reads that table. The raw settlement fact is still available for batch-level analysis, but no consumer has to remember the sub-query.

**Proof:** `test_one_to_many_settlement_is_aggregated_before_transaction_level_maths` asserts `settlement_count = 2`, `settled_amount = 10,000`, `transaction_amount = 10,000`, `gap = 0`, state `FULLY_SETTLED`. Query 7 versus query 8 in `sql/91_reconciliation_and_dq_queries.sql` shows both numbers side by side.

---

## Issue 2 — Late-arriving events (business time versus processing time)

**What it looks like**
```
event_ts      10:02:15      <- when it happened in the business
ingestion_ts  10:08:41      <- when the platform received it
```
390 late events and 330 ingestion SLA breaches in the sample.

**Why it is dangerous.** If the reporting date comes from `ingestion_ts`, a 23:58 transaction received at 00:06 lands on the wrong day, and every daily total is quietly wrong at the boundary. If lateness is treated as an error, legitimate money gets rejected.

**How it is handled.**
- Business time (`transaction_ts`, `event_ts`, `settlement_ts`) drives every date key, every KPI and every SLA measurement.
- Processing time (`ingestion_ts`, `_ingested_at`) drives only pipeline control: watermarks, deduplication order, lateness flags.
- `ingestion_lag_sec` is stored on every event. Lag above 5 minutes is a WARN (EVT-005); above 15 minutes it is a business exception (EVT-006) surfaced in the data quality panel. Negative lag (producer clock skew) has its own rule, EVT-007.
- Lateness never rejects a record.

**Proof:** `test_late_arriving_event_is_flagged_on_business_time` asserts a 511 second lag is late but not a breach, while 1,800 seconds is a breach.

---

## Issue 3 — Out-of-order events

**What it looks like**
```
arrival order:   AUTHORIZED 10:01, SETTLED 10:03, CREATED 10:00
```

**Why it is dangerous.** Any logic that assumes "the last row I received is the current state" will mark a settled transaction as created. Sequencing by arrival is the classic event-processing defect.

**How it is handled.** The platform never infers state from arrival order. Lifecycle state is derived from the *set* of events ordered by `event_ts`, and settlement state is derived from the settlement records themselves rather than from the event stream. Both orderings are retained — `event_ts` for business truth, `ingestion_ts` for operational analysis — so "did we receive it out of order?" is answerable without corrupting the business answer.

**Proof:** `test_out_of_order_events_are_ordered_by_business_time` asserts that ordering T3's events by `event_ts` gives CREATED → AUTHORIZED, while ordering by `ingestion_ts` gives AUTHORIZED → CREATED. Both are true; only the first is used for reporting.

---

## Issue 4 — Merchant risk changes (point-in-time correctness)

**What it looks like**
```
M100   LOW     Jan-Mar
       HIGH    Apr-Jun
       MEDIUM  Jul onwards
```

**Why it is dangerous.** Joining on `merchant_id` alone returns three rows per transaction — a second fan-out, this time multiplying the transaction by its risk history. Joining to "current risk" is worse: it is a single row, so it looks correct, but it rewrites history. A February transaction reviewed under today's MEDIUM classification is a compliance finding.

**How it is handled.** `gold.dim_merchant` is SCD Type 2 with a surrogate key per validity period, and the fact join is a band join on the transaction date:
```sql
ON m.merchant_id = t.merchant_id
AND t.transaction_date BETWEEN m.effective_from AND m.effective_to
```
`effective_to` NULL is normalised to `9999-12-31` at Silver. In PostgreSQL an exclusion constraint makes overlapping windows physically impossible; in the reference build overlaps are detected as business exception MER-004 and asserted by a test.

**Proof:** `test_merchant_risk_is_applied_as_of_the_transaction_date` asserts LOW for a 10 Feb transaction and MEDIUM for a 1 Sep transaction on the same merchant. `test_scd2_merchant_windows_do_not_overlap` and `test_exactly_one_current_row_per_merchant` guard the dimension.

---

## Issue 5 — Data quality problems, classified rather than deleted

The brief lists seven problems and asks the engineer to decide which are rejectable, quarantine, warning or business exception. That classification decision is the deliverable:

| Problem in the data | Rule | Severity | Disposition | Reasoning | Sample count |
|---|---|---|---|---|---|
| Missing merchant IDs | TXN-005 | HIGH | QUARANTINE | the money is real but cannot be attributed or settled to anyone; a human must map it | 26 |
| Duplicate event IDs | EVT-004 | MEDIUM | WARN | a re-delivery, not an error; keep the earliest arrival and count the rest | 24 |
| Negative settlement amounts | STL-004 | HIGH | QUARANTINE | either a reversal the model does not yet support or a source defect; it must not reduce settled value until operations confirms which | 25 |
| Transactions without settlement | BR-05 | — | BUSINESS_EXCEPTION | perfectly valid data describing the exact problem the platform exists to report | 243 UNSETTLED |
| Settlements without a matching transaction | STL-006 | HIGH | BUSINESS_EXCEPTION | loaded, flagged orphan, excluded from rates; re-evaluated every run in case the parent is simply late | 94 |
| Invalid currency values | TXN-006 | HIGH | QUARANTINE | no agreed FX source, so converting would be inventing a number | 39 |
| Events arriving after the SLA | EVT-006 | MEDIUM | BUSINESS_EXCEPTION | the data is fine, the *service* breached; it belongs in an operations report, not a bin | 330 |
| Unknown merchant ID (not in master) | TXN-010 | MEDIUM | WARN | mapped to `merchant_key = -1` so platform totals still reconcile, flagged for master-data fix | 41 |
| Blank id / unparsable timestamp / non-numeric or negative amount | TXN-001..004 | CRITICAL | REJECT | structurally unusable; cannot be keyed or measured | 0 in sample |

**The principle behind the classification.** Ask two questions of every bad record: *can the platform compute anything correct from it?* and *is the defect in the data or in the business?* REJECT is for records that cannot be keyed. QUARANTINE is for real records that need a human. WARN is for records that are usable once a documented rule is applied. BUSINESS_EXCEPTION is for records that are perfectly valid and are telling you something is wrong in the business — those are the platform's output, not its waste.

**Nothing silently disappears.** Every rejected or quarantined record is written to `ctl.dq_quarantine` with its rule, severity, reason and original payload. Every rule's failure count per batch is written to `ctl.dq_rule_result`, exposed at `/api/v1/data-quality`, shown on the dashboard, and gated in CI by `scripts/dq_gate.py` (build fails on any CRITICAL failure or quarantine above 2% of source rows).

The completeness invariant is asserted on every commit:
```
source rows (6,988) = loaded (6,923) + quarantined (65) + deduplicated (0)
```

---

## The answer to the Head of Payments' question

With all five issues handled, the ₹3.29 Cr gap on the sample week decomposes into causes:

| Cause | Transactions | Gap value | Share of gap |
|---|---|---|---|
| Never settled (no settlement record at all) | 170 | ₹1.64 Cr | 49.9% |
| Settlement pending | 103 | ₹0.92 Cr | 28.1% |
| Settlement failed | 68 | ₹0.67 Cr | 20.5% |
| Partially settled | 30 | ₹0.05 Cr | 1.5% |
| Fully settled | 4,471 | ₹0 | 0% |

Plus, reported separately because they are not part of the rate: 94 orphan settlements, 26 unattributable transactions, 39 invalid-currency transactions, 330 events that breached the ingestion SLA. That is the difference between "we lost ₹2.2 Cr somewhere" and a work queue.
