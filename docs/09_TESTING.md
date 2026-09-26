# Task 4 Parts A, B and C — Testing

> Deliverable for: *"TDD / unit testing, data model tests, API contract tests."*
> 64 tests, all passing, run in under 3 seconds: `pytest`.

---

## 1. Test strategy

| Layer | File | Tests | Question it answers |
|---|---|---|---|
| Transformation / TDD | `tests/test_transformations.py` | 20 | does each business rule behave as specified? |
| Data model | `tests/test_data_model.py` | 18 | is the warehouse structurally correct and reconciled? |
| API contract | `tests/test_api_contract.py` | 16 | does the interface honour its contract? |
| Security | `tests/test_security.py` | 10 | can the system be misused or leak? |

The fixture is the point. `tests/conftest.py` holds a hand-built 12-transaction dataset with every hidden problem in it, and the pipeline runs against it once per session. Because the dataset is small enough to reason about on paper, the expected KPIs are hand-calculated and asserted exactly — ₹36,000 successful volume, ₹20,000 settled, 55.56% settlement rate, 66.67% SLA rate, 2 unsettled. A test that asserts a number someone computed by hand is a specification; a test that asserts whatever the code produced is a screenshot.

## 2. Part A — TDD / unit tests

Every case the brief lists, plus the ones the data actually contains:

| Required case | Test | Asserted behaviour |
|---|---|---|
| successful transaction | `test_successful_transaction_is_loaded_and_marked` | loaded, `is_successful = true`, amount preserved as Decimal |
| failed transaction | `test_failed_transaction_is_excluded_from_settlement_scope` | not in reconciliation, still in `fact_transaction` |
| duplicate event | `test_duplicate_event_id_is_deduplicated_keeping_earliest_ingestion` | one row survives, earliest ingestion kept, EVT-004 counts one |
| missing merchant | `test_missing_merchant_is_quarantined_not_dropped` | absent from Silver, present in quarantine as TXN-005 |
| unmatched settlement | `test_unmatched_settlement_is_flagged_orphan_and_excluded_from_kpis` | `is_orphan = true`, STL-006 logged, contributes nothing |
| negative settlement | `test_negative_settlement_is_quarantined` | absent from Silver, quarantined as STL-004 |
| late event | `test_late_arriving_event_is_flagged_on_business_time` | 511 s = late, not breach; 1,800 s = breach |
| settlement calculation | `test_one_to_many_settlement_is_aggregated_before_transaction_level_maths` | 2 settlements, 10,000 settled, counted once |

Beyond the required list: negative transaction amount rejected, invalid currency quarantined, unknown merchant mapped to `merchant_key = -1`, duplicate transaction deduplicated keeping the latest, out-of-order events ordered by business time, all five settlement states, SLA measured to the first settled record, point-in-time risk, hand-calculated daily aggregate, idempotent rerun, incremental delta, and a late settlement reopening a closed day.

Two tests deserve calling out in an interview:

```python
def test_daily_aggregate_matches_hand_calculated_kpis(con):
    # ₹36,000 successful, ₹20,000 settled, gap ₹16,000,
    # SLA 2 of 3 eligible, 2 unsettled, 6 successful transactions
```
```python
def test_late_arriving_settlement_reopens_a_closed_day(tmp_path):
    # T5 was UNSETTLED on 01 Sep; a settlement arrives on 03 Sep;
    # only 1 transaction is recomputed; 01 Sep settled rises 20,000 -> 23,000
```
The first proves the KPI maths. The second proves the incremental design, which is the part most implementations get wrong.

## 3. Part B — data model tests

| Check | Test | SQL |
|---|---|---|
| Grain uniqueness on every fact and dimension | `test_grain_is_unique` (parametrised over 7 tables) | `GROUP BY key HAVING count(*) > 1` → expect 0 rows |
| Aggregate grain is date × merchant | `test_aggregate_grain_is_date_by_merchant` | same shape on the composite key |
| Transactions reference an existing merchant | `test_transactions_reference_an_existing_merchant_dimension_row` | LEFT JOIN, expect no NULL dimension |
| Transactions reference date and customer | `test_transactions_reference_existing_date_and_customer` | LEFT JOIN both dimensions |
| Non-orphan settlements reference a transaction | `test_non_orphan_settlements_reference_an_existing_transaction` | LEFT JOIN with `NOT is_orphan` |
| Reconciliation covers every successful transaction exactly once | `test_reconciliation_covers_every_successful_transaction_exactly_once` | missing = 0 and extra = 0 |
| Settlement totals tie between facts | `test_settlement_totals_reconcile_between_fact_and_reconciliation` | sum(SETTLED, non-orphan) == sum(settled_amount) |
| Aggregate ties to reconciliation | `test_aggregate_totals_reconcile_with_the_reconciliation_fact` | both value columns |
| Nothing silently dropped | `test_source_rows_are_either_loaded_or_recorded_as_exceptions` | source == loaded + quarantined + deduplicated |
| Rates stay in bounds | `test_settlement_gap_is_never_negative_at_aggregate_level` | no row outside 0–100 |
| SCD2 windows never overlap | `test_scd2_merchant_windows_do_not_overlap` | self-join on overlapping ranges |
| One current row per merchant | `test_exactly_one_current_row_per_merchant` | `HAVING count(*) <> 1` |

The settlement reconciliation the brief asks for — successful transactions + settlements + unsettled transactions tying to the totals — is query 9 in `sql/91_reconciliation_and_dq_queries.sql` and is asserted by the two reconciliation tests above. Running it on the sample week gives: ₹55.91 Cr successful, ₹51.36 Cr settled, ₹0.92 Cr pending, ₹0.67 Cr failed settlements, 243 unsettled transactions, 35 partially settled.

## 4. Part C — API contract tests

| Requirement | Test |
|---|---|
| 200 valid request | `test_settlement_summary_returns_200_for_a_valid_request` |
| 400 invalid date | `test_invalid_date_returns_400`, `test_inverted_date_range_returns_400` |
| 404 unknown merchant | `test_unknown_merchant_returns_404` |
| 422 invalid parameter | `test_invalid_parameter_returns_422`, `test_missing_required_parameter_returns_422` |
| `settlement_rate >= 0` and `<= 100` | `test_settlement_rate_is_bounded` |
| Response shape | `test_settlement_summary_returns_200_for_a_valid_request` checks all six required fields |
| KPI correctness end to end | `test_settlement_summary_matches_expected_kpis` — the API returns the same hand-calculated numbers the warehouse holds |
| Auth | `test_missing_api_key_returns_401`, `test_wrong_api_key_returns_401` |
| Merchant scoping | `test_merchant_filter_scopes_the_result` |
| Exception report shape and thresholds | `test_merchant_exceptions_contract` |
| Ordering and limits | `test_daily_trend_is_ordered_and_scoped`, `test_top_merchant_gaps_respects_limit_and_ordering` |
| Published contract | `test_openapi_contract_is_published` |

## 5. How TDD was actually applied

The sequence was: write the Gherkin scenario → write the failing test with the hand-calculated expectation → implement the transformation → watch it pass → refactor. The one-to-many settlement rule is the clearest example: the test asserting `settled_amount = 10,000` and `transaction_amount = 10,000` was written first and failed with 20,000 against a naive join, which is exactly the defect the reconciliation table exists to prevent.

## 6. Running the suite

```bash
make test                          # pytest --cov=src --cov-report=term-missing
pytest tests/test_data_model.py -v # one layer
pytest -k settlement -v            # one theme
python -m scripts.dq_gate --max-critical 0 --max-quarantine-pct 2.0
```

In CI (`.gitlab-ci.yml`): lint → unit and contract tests with an 80% line-coverage gate → a data quality gate that runs the pipeline on generated data and fails the build on any CRITICAL rule failure or quarantine above 2% → SAST, dependency scan and secret detection → build → deploy with a smoke test.

## 7. What is deliberately not tested, and why

- Third-party library behaviour (FastAPI routing, DuckDB SQL execution) — testing a dependency's own contract adds maintenance without adding signal.
- The dashboard's visual layout — worth a Playwright smoke test in a longer engagement; the API contract tests already guarantee the data it renders.
- Load and soak testing — designed for (pre-aggregated serving table, bounded ranges, capped limits) but out of scope for a sprint. A k6 script against `/api/v1/settlement-summary` at the 366 day boundary would be the first addition.
