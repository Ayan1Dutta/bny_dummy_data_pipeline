# Master Guide — how to present this, and what to say

Read this first. It maps every sprint task to its deliverable, gives you the numbers to quote, and rehearses the questions you will actually be asked.

---

## 1. Task → deliverable map

| Sprint task | Document | Code |
|---|---|---|
| Task 1.1 Business specification | `01_BUSINESS_SPECIFICATION.md` | — |
| Task 1.2 Technical specification | `02_TECHNICAL_SPECIFICATION.md` | — |
| Task 1.3 Gherkin (≥5 scenarios) | `03_GHERKIN_ACCEPTANCE_SCENARIOS.md` | `features/` (25 scenarios) |
| Task 2 Data model, grain, DDL, keys, indexes | `04_DATA_MODEL.md` | `sql/01-04`, `sql/90_postgres_production_ddl.sql` |
| Task 2 Pipeline, bronze/silver/gold, incremental | `05_DATA_PIPELINE.md` | `src/ingestion/`, `src/transform/`, `src/pipeline/` |
| Section 3 Hidden data problems | `06_HIDDEN_DATA_PROBLEMS.md` | `src/quality/rules.py` |
| Task 3 A+B API | `07_API.md` | `src/api/` |
| Task 3 C Front-end | `08_FRONTEND.md` | `frontend/index.html` |
| Task 4 A+B+C Testing | `09_TESTING.md` | `tests/` (64 tests) |
| Task 4 D Security | `10_SECURITY.md` | `src/api/security.py`, `sql/90` grants, `tests/test_security.py` |
| Task 4 E Deployment | `11_DEPLOYMENT_GITLAB_AWS.md` | `.gitlab-ci.yml`, `Dockerfile`, `infra/` |
| How to run and demo it | `12_RUNBOOK.md` | `Makefile`, `scripts/` |

## 2. The 90 second opening

> "The Head of Payments could not explain a ₹2.2 Cr difference because a total cannot tell you *why*. So the platform does two things: it separates the five possible causes, and it never silently drops a record.
>
> On the sample week: ₹43.26 Cr of successful transactions, ₹39.97 Cr settled, a ₹3.29 Cr gap at a 92.4% settlement rate and 91.2% SLA. That gap is 49.9% transactions with no settlement at all, 28.1% pending, 20.5% failed settlements, 1.5% partial — and thirteen merchants carry most of it.
>
> The two engineering decisions everything else follows from: settlements are collapsed to transaction grain once, in the pipeline, because a direct join inflates the numbers by ₹1.59 Cr on this dataset; and business time drives reporting while processing time drives only pipeline control, so late and out-of-order data corrects the day it belongs to instead of the day it arrived."

## 3. Numbers to have ready

| Figure | Value |
|---|---|
| Successful transaction value, 01–07 Sep | ₹43.26 Cr (₹432,558,048.71) |
| Settled value | ₹39.97 Cr (₹399,686,115.82) |
| Settlement rate / SLA rate | 92.4% / 91.22% |
| Settlement gap | ₹3.29 Cr (₹32,871,932.89), 4,842 successful transactions, 170 unsettled |
| Gap decomposition | 49.9% unsettled, 28.1% pending, 20.5% settlement failed, 1.5% partial |
| Merchants breaching thresholds | 13 of 31; worst is M1009 at 69.8% settlement, 63.8% SLA, ₹54.4 L gap |
| Fan-out damage | naive join ₹57.49 Cr vs correct ₹55.91 Cr → ₹1.59 Cr of imaginary money |
| Transactions with split settlements | 481 |
| Data quality | 65 transactions quarantined, 25 settlements quarantined, 94 orphans, 390 late events, 330 ingestion SLA breaches, 24 duplicate events, 41 unknown merchants |
| Completeness invariant | 6,988 source rows = 6,923 loaded + 65 quarantined + 0 deduplicated |
| Tests | 64 passing, under 3 seconds |
| Fixture KPIs (hand-calculated) | ₹36,000 volume, ₹20,000 settled, 55.56% rate, 66.67% SLA, 2 unsettled |

## 4. The five hidden problems, in one line each

1. **One-to-many settlement** — a direct join multiplies the transaction by its settlement count; collapsed once into `fact_settlement_reconciliation`, worth ₹1.59 Cr of error on this dataset.
2. **Late-arriving events** — business time drives every date and KPI; processing time drives only watermarks, dedup order and lateness flags. Lateness is flagged, never rejected.
3. **Out-of-order events** — state is derived from the *set* of events ordered by `event_ts`, never from arrival order; both orderings are retained so lateness is still measurable.
4. **Merchant risk changes** — SCD Type 2 with a band join on the transaction date; a February transaction is classified LOW even though the merchant is MEDIUM today.
5. **Data quality** — four dispositions (REJECT / QUARANTINE / WARN / BUSINESS_EXCEPTION), every decision justified, every record recoverable with its original payload, and the count invariant asserted on every commit.

## 5. Questions you will be asked, and the answers

**"Why not just join transactions to settlements?"**
Because a transaction can have several settlement rows, so the join repeats the transaction amount once per settlement. On this dataset that reports ₹57.49 Cr instead of ₹55.91 Cr. I aggregate settlements to transaction grain once, in the pipeline, so no consumer can make that mistake. Query 7 and query 8 in `sql/91` show both numbers.

**"What is the grain of each fact table?"**
`fact_transaction` one payment attempt; `fact_settlement` one settlement record; `fact_payment_event` one lifecycle event; `fact_settlement_reconciliation` one successful transaction with settlements pre-aggregated; `agg_daily_merchant_settlement` one date × merchant. Grain uniqueness is a parametrised test over seven tables.

**"Why keep failed transactions at all?"**
Because the funnel matters. The value KPI uses SUCCESS only, but attempt counts tell operations whether a merchant's problem is settlement or authorisation. Dropping them at the Silver boundary would throw away the distinction permanently.

**"A settlement arrives two days late. What happens?"**
The pipeline collects the transaction ids touched by the batch — including the parents of new settlements — recomputes reconciliation for exactly those, then rebuilds only the affected date × merchant aggregate partitions. The transaction moves from UNSETTLED to FULLY_SETTLED and the *original* day's settled value increases. There is a test that does exactly this.

**"Why quarantine a missing merchant but keep an unknown merchant?"**
Different failures. A missing `merchant_id` cannot be attributed to anyone — no merchant KPI can include it and no one can be paid, so it needs a human. An unknown `merchant_id` is a master-data gap: the money is real and attributable to *something*, so it loads against an UNKNOWN dimension member, platform totals still reconcile, and rule TXN-010 flags it for the master-data team.

**"How do you know a rerun is safe?"**
Every write is `INSERT OR REPLACE` on a declared key or a scoped delete-and-insert; files are skipped by content hash; stages read by watermark. `test_pipeline_rerun_is_idempotent` snapshots the warehouse, reruns, and asserts nothing changed.

**"How would this scale to 50 million rows a day?"**
The layering, grain and incremental strategy do not change — only the engine. PostgreSQL partitioned monthly handles the next order of magnitude; beyond that, `infra/aws/glue_job_bronze_to_silver.py` is the same Silver logic in PySpark against Delta on S3. The model already separates business and processing time, so a streaming front end replaces ingestion without touching Silver or Gold.

**"Why DuckDB?"**
So the whole platform runs with `pip install` and no server, while keeping real SQL, real constraints and columnar performance. The production DDL for RDS PostgreSQL ships alongside it and the model is identical; only physical design differs — sequences, declarative partitions, and a GiST exclusion constraint that makes overlapping risk windows impossible.

**"Where is SQL injection prevented?"**
Bound parameters everywhere, plus a regex allow-list on `merchant_id`, plus a read-only database role with SELECT on three objects. And a test that scans every SQL string in the source and fails the build if user input is interpolated — so the rule survives after I leave.

**"What does the API return for an unknown merchant?"**
404. A malformed date is 400, a bad parameter shape is 422, no key is 401. All five are asserted in the contract tests, and the date parsing is explicit precisely so 400 and 422 stay distinguishable.

**"What would you do next, with more time?"**
Three things: a bridge from settlement batch to the finance general ledger so the reconciliation extends to the GL; per-merchant SLA policies instead of one global 30 minutes; and a streaming ingestion path so the dashboard is minutes fresh rather than fifteen. I would also add pytest-bdd so the `.feature` files execute directly rather than through mirrored tests.

## 6. Where to click if they want to see code

| They ask about | Open |
|---|---|
| The fan-out fix | `src/transform/gold.py` → `build_reconciliation`, then `sql/91` queries 7 and 8 |
| Data quality decisions | `src/quality/rules.py` (28 rules with severity and disposition) |
| Incremental logic | `src/transform/gold.py` → `affected_transactions`, `build_aggregate` |
| Point-in-time risk | `src/transform/gold.py` → `load_fact_transaction` band join |
| Validation and routing | `src/transform/silver.py` → `_quarantine`, `_business_exception` |
| API validation | `src/api/main.py` → `parse_window`, `validate_merchant` |
| Security tests | `tests/test_security.py` |
| CI gates | `.gitlab-ci.yml` |
| AWS design | `infra/terraform/main.tf`, `infra/aws/` |

## 7. What to say about trade-offs (they always ask)

- **Quarantining missing-merchant transactions** keeps merchant KPIs honest but leaves that money outside merchant totals. Mitigated by reporting it prominently and by the completeness invariant. The alternative — an UNATTRIBUTED bucket inside the merchant KPIs — was rejected because it makes every merchant total slightly wrong instead of one number visibly incomplete.
- **Excluding unsettled transactions from the SLA denominator** avoids double-punishing the same failure, since they are already the largest part of the gap. It is an assumption, documented as A7, and reversible in one CASE expression.
- **A pre-aggregated serving table** buys latency at the cost of a recompute step. Justified because the API must be fast and the recompute is scoped to affected keys.
- **One global SLA threshold** is simpler than per-merchant policies but wrong for merchants with contractual terms. It is configuration, not code, so it is a data change when the business is ready.
