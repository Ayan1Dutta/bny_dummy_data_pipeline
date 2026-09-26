# 16 — Incident Report INC-2026-0925-01

| Field | Value |
|---|---|
| Incident ID | INC-2026-0925-01 |
| Date | 2026-09-25 |
| Application | Settlement Intelligence Platform — Settlement API and dashboard |
| Production version | GREEN v2.0.0 (release `v2`) — rolled back to BLUE v1.0.0 |
| Severity | SEV-2: wrong financial KPI shown to operations, no data loss |
| Status | Resolved by rollback; fix pending |

## What happened
After the blue-green cutover to v2.0.0, the settlement rate on the dashboard rose from **92.40%** to **99.21%** for 2026-09-01..07. Nineteen merchants' drill-downs failed with HTTP 500. `/health` returned 200 throughout.

## Business impact

| KPI | Correct (v1) | Shown by v2 | Effect |
|---|---|---|---|
| Settled amount | ₹39.97 Cr | ₹42.91 Cr | Overstated by ₹2.94 Cr |
| Settlement rate | 92.40% | 99.21% | Platform looks healthy when it is not |
| Settlement gap | ₹3.29 Cr | ₹0.34 Cr | ₹2.94 Cr of unsettled money hidden |
| Merchant view | 30 merchants correct | 19 HTTP 500, 11 wrong values | Operations cannot investigate merchants |
| SLA rate | 91.22% | 91.22% | Not affected |

Classification: **query problem introduced by a deployment**. Not an application crash, data problem, configuration or pipeline problem — the warehouse and pipeline were correct and the reconciliation gate confirmed the facts still gave 92.40%.

Why this is worse than the trainer's 103.7%: on this dataset the inflated total stays below 100%, so a simple "rate ≤ 100%" check on the total passes. Only per-merchant and fact-level reconciliation reveal the problem.

## Detection method
1. KPI anomaly: dashboard rate vs recorded baseline (+6.81 pp).
2. Reconciliation gate against production: 4 checks FAIL — platform mismatch with facts, 19 merchant 500s, 11 merchants disagree with facts, baseline drift.
3. Artifact integrity warning: GREEN container runs image `b970acf540b9`, but release v2 was approved (built after 106/106 tests passed) as `2d0d9d3195bf`.
4. Health check: passed — confirms it is useless for this class of failure.

## Technical root cause
`src/api/repository.py` in v2 changed `settlement_summary` and `daily_trend` to join `fact_transaction` to `fact_settlement` **before** aggregating and to sum `t.amount` for every `SETTLED` settlement row. A transaction split into two settlements is counted twice, and a partial settlement counts as fully settled. For merchants whose inflated rate exceeded 100%, the Pydantic response schema (`settlement_rate ≤ 100`) rejected the response, producing HTTP 500 instead of publishing an impossible number.

## Evidence
- `evidence/10_incident/timeline.md` — controller log
- `evidence/10_incident/detection_reconciliation.json` — failing gate
- `python -m deploy.bluegreen diff v1 v2` — the changed query
- `python -m deploy.bluegreen logs green` — pydantic `ValidationError` entries (rate above 100 rejected)
- `evidence/10_incident/*_rollback.json` — before/after values

## Timeline (drill run, compressed; times from the controller log)

| Time | Event |
|---|---|
| 08:00:23 | GREEN v2.0.0 pre-cutover checks passed (health, DB, pipeline, 106/106 tests, smoke 11/11, reconciliation) |
| 08:00:26 | Cutover BLUE v1.0.0 → GREEN v2.0.0 |
| 08:00:27 | Post-cutover verification passed |
| 08:00:32 | GREEN restarted with changed code — artifact integrity warning |
| 08:00:34 | KPI anomaly detected (99.21% vs 92.40%) |
| 08:00:36 | Investigation: reconciliation FAIL, status TAMPERED, diff, logs |
| 08:00:38 | Root cause identified; decision: rollback |
| 08:00:38 | Rollback initiated; 100% traffic → BLUE v1.0.0 |
| 08:00:39 | Smoke tests passed via load balancer |
| 08:00:39 | Business KPI reconciled: 92.40% = facts |

## Decision: rollback, not fix-forward

| Factor | Assessment |
|---|---|
| Impact | Wrong money figure on the live operations dashboard; merchant investigation blocked |
| Rollback safety | BLUE running, healthy, on the approved warehouse, verified at baseline |
| Rollback time | Under a minute (listener switch) |
| Fix-forward risk | Requires a query change and a new regression test; untested code in production |
| Data risk | None — the warehouse was correct; only the read path was wrong |

When the known-good version is running and the fault is in code, rolling back is cheaper and safer than debugging in production.

## Validation after rollback
Settlement rate 99.21% → **92.40%**, matching the independent recompute from facts and the baseline. Smoke test 11/11. Traffic served by BLUE (`X-Served-By: blue`, `X-Release-Version: 1.0.0`).

## 5 Whys
1. **Why was the settlement rate wrong?** Settled amount was inflated by ₹2.94 Cr.
2. **Why?** The v2 query joined transactions to settlements before aggregating and summed the transaction amount per settled row.
3. **Why did that reach production?** It was changed in the running release after approval; the approved v2 had passed every gate.
4. **Why was it not stopped immediately?** Production traffic was not blocked on the artifact integrity warning, and the only automatic live check was `/health`.
5. **Why?** Business-KPI reconciliation ran at pre-cutover and rollback, but not continuously against live production.

Note: in a normal delivery path this defect would have been blocked. Running the test suite against the altered v2 fails 12 of 106 tests, including `test_settlement_summary_matches_expected_kpis` and both mandatory multi-settlement tests, and cutover refuses a changed artifact.

## Preventive actions

| # | Control | Status | Owner | Due |
|---|---|---|---|---|
| 1 | Regression tests for one transaction → multiple settlements, with a precondition that the fixture keeps a split | Done (`tests/test_business_rules.py`) | Data Engineering | Done |
| 2 | Reconciliation gate: API vs independent fact recompute, settled ≤ transacted per merchant, baseline drift | Done, runs in precheck and rollback | Data Engineering | Done |
| 3 | Artifact integrity: image ID and config fingerprint verified at every start; cutover blocked on mismatch | Done | Platform | Done |
| 4 | Continuous KPI anomaly monitoring: run the reconciliation gate every 15 minutes against production and page on FAIL | Open | SRE | 2026-10-09 |
| 5 | Mandatory SQL/data-model review for changes to Gold-layer metric queries (CODEOWNERS on `src/api/repository.py`, `sql/03_gold.sql`) | Open | Tech Lead | 2026-10-02 |
| 6 | Blue-green smoke on business KPIs, not only `/health` | Done (`scripts/smoke_test.py`) | Data Engineering | Done |
| 7 | Keep response-schema bounds (`le=100`) as defence in depth | Done | API | Done |
