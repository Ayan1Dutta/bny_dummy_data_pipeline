# Task 1.3 — Gherkin Acceptance Scenarios

> Deliverable for: *"Write at least 5 acceptance scenarios."*
> 25 scenarios are shipped across four feature files in `features/`. Each one maps to a real test in `tests/`, so the Gherkin is not decoration — it is the specification the suite actually enforces.

---

## Why these scenarios and not others

The scenarios were written before the implementation, one per hidden data problem plus one per KPI edge case. Anything that could be interpreted two ways became a scenario, because a scenario is how a business rule survives a handover.

| Feature file | Covers | Scenarios |
|---|---|---|
| `features/settlement_reconciliation.feature` | KPI correctness and settlement states | 7 |
| `features/data_quality.feature` | reject / quarantine / warn / business exception handling | 7 |
| `features/late_and_out_of_order_events.feature` | business time vs processing time, late partition recompute | 5 |
| `features/merchant_risk_and_api.feature` | SCD2 point-in-time risk and API contract | 8 |

## The five headline scenarios

### 1. Identify an unsettled successful transaction (the scenario given in the brief)

```gherkin
Scenario: Identify an unsettled successful transaction
  Given a successful payment "T5001" of 3000.00 INR exists for merchant "M100" on "2026-09-01"
  And no settlement record exists for "T5001"
  When the settlement pipeline runs
  Then transaction "T5001" should be classified as "UNSETTLED"
  And it should appear in the settlement exception report
  And the settlement gap for merchant "M100" on "2026-09-01" should include 3000.00
```
Implemented by: `test_settlement_states_cover_pending_partial_and_unsettled`, `test_daily_aggregate_matches_hand_calculated_kpis`.

### 2. Aggregate a one-to-many settlement before transaction level maths

```gherkin
Scenario: Aggregate a one-to-many settlement before transaction level maths
  Given a successful payment "T1001" of 10000.00 INR exists
  And settlement "S5001" of 8000.00 with status "SETTLED" exists for "T1001"
  And settlement "S5002" of 2000.00 with status "SETTLED" exists for "T1001"
  When the settlement pipeline runs
  Then the reconciled settled amount for "T1001" should be 10000.00
  And the transaction amount for "T1001" should be counted exactly once
  And transaction "T1001" should be classified as "FULLY_SETTLED"
```
Implemented by: `test_one_to_many_settlement_is_aggregated_before_transaction_level_maths`. This is the scenario that catches the fan-out defect; in the sample dataset the naive join overstates transaction value by ₹1.59 Cr.

### 3. Risk level is applied as at the transaction date

```gherkin
Scenario: Risk level is applied as at the transaction date
  Given merchant "M100" has risk "LOW" from "2026-01-01" to "2026-03-31"
  And merchant "M100" has risk "HIGH" from "2026-04-01" to "2026-06-30"
  And merchant "M100" has risk "MEDIUM" from "2026-07-01" onwards
  When a transaction for "M100" on "2026-02-10" is processed
  Then that transaction should be classified under risk level "LOW"
  And a transaction for "M100" on "2026-09-01" should be classified under risk level "MEDIUM"
```
Implemented by: `test_merchant_risk_is_applied_as_of_the_transaction_date`.

### 4. A settlement without a matching transaction is flagged as an orphan

```gherkin
Scenario: A settlement without a matching transaction is flagged as an orphan
  Given a settlement "S9001" references transaction "T9999"
  And no transaction "T9999" exists in the platform
  When the settlement pipeline runs
  Then settlement "S9001" should be loaded and flagged as an orphan
  And it should be recorded as a business exception under rule "STL-006"
  And it should not contribute to any merchant settlement rate
```
Implemented by: `test_unmatched_settlement_is_flagged_orphan_and_excluded_from_kpis`. Note the deliberate choice: loaded and flagged, not dropped — the orphan may be the early half of a transaction that has not arrived yet, and the flag is re-evaluated on every run.

### 5. A settlement arriving after the reporting day is closed reopens that day

```gherkin
Scenario: A settlement arriving after the reporting day is closed reopens that day
  Given transaction "T8005" of 3000.00 on "2026-09-01" was previously classified as "UNSETTLED"
  And a settlement of 3000.00 for "T8005" arrives on "2026-09-03"
  When the incremental pipeline runs
  Then transaction "T8005" should be reclassified as "FULLY_SETTLED"
  And only the affected date and merchant partition should be recomputed
  And the settled amount reported for "2026-09-01" should increase by 3000.00
```
Implemented by: `test_late_arriving_settlement_reopens_a_closed_day`. This scenario is the reason the pipeline recomputes by affected key rather than by "yesterday".

## Traceability matrix

| Scenario theme | Feature file | Test |
|---|---|---|
| Unsettled transaction | settlement_reconciliation | `test_settlement_states_cover_pending_partial_and_unsettled` |
| One-to-many settlement | settlement_reconciliation | `test_one_to_many_settlement_is_aggregated_before_transaction_level_maths` |
| Partial settlement | settlement_reconciliation | same test, `PARTIALLY_SETTLED` assertion |
| Pending not counted as settled | settlement_reconciliation | `test_settlement_states_cover_pending_partial_and_unsettled` |
| SLA met / breached | settlement_reconciliation | `test_sla_is_measured_from_transaction_to_first_settled_record` |
| Failed and reversed excluded | settlement_reconciliation | `test_failed_transaction_is_excluded_from_settlement_scope` |
| Orphan settlement | data_quality | `test_unmatched_settlement_is_flagged_orphan_and_excluded_from_kpis` |
| Missing merchant quarantined | data_quality | `test_missing_merchant_is_quarantined_not_dropped` |
| Negative settlement quarantined | data_quality | `test_negative_settlement_is_quarantined` |
| Duplicate event deduplicated | data_quality | `test_duplicate_event_id_is_deduplicated_keeping_earliest_ingestion` |
| Unknown merchant mapped to UNKNOWN | data_quality | `test_unknown_merchant_is_loaded_against_the_unknown_dimension_member` |
| Invalid currency quarantined | data_quality | `test_invalid_currency_is_quarantined` |
| Nothing silently dropped | data_quality | `test_source_rows_are_either_loaded_or_recorded_as_exceptions` |
| Late arrival warning vs breach | late_and_out_of_order_events | `test_late_arriving_event_is_flagged_on_business_time` |
| Out-of-order arrival | late_and_out_of_order_events | `test_out_of_order_events_are_ordered_by_business_time` |
| Late settlement reopens a day | late_and_out_of_order_events | `test_late_arriving_settlement_reopens_a_closed_day` |
| Point-in-time risk | merchant_risk_and_api | `test_merchant_risk_is_applied_as_of_the_transaction_date` |
| No overlapping risk windows | merchant_risk_and_api | `test_scd2_merchant_windows_do_not_overlap`, `test_exactly_one_current_row_per_merchant` |
| API 200 / 400 / 404 / 422 / 401 | merchant_risk_and_api | `tests/test_api_contract.py` |
| Exception report contents | merchant_risk_and_api | `test_merchant_exceptions_contract` |

## Running them as executable specifications

The scenarios are currently enforced by the pytest suite. To run the `.feature` files directly, add `pytest-bdd` and one step file:

```bash
pip install pytest-bdd
pytest tests/test_features.py          # scenarios("../features")
```
The step definitions map one-to-one onto the fixtures already in `tests/conftest.py`, so no production code changes.
