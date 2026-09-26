# 17 — Executive Presentation (8 slides, ~9 minutes)

## Slide 1 — Business problem (1 min)
BNY processes ₹43.26 Cr of merchant payments a week (sample). Operations could not see how much was actually settled, which merchants were late, or trust the numbers: split settlements double-count, events arrive late and out of order, merchant risk changes over time.
**Say:** "Without one trusted settlement number, every investigation starts with an argument about the data."

## Slide 2 — Architecture (1 min)
Source CSVs → Bronze (raw, hashed, audited) → Silver (typed, deduplicated, 28 DQ rules, quarantine) → Gold (facts at transaction grain, SCD2 merchant, reconciliation fact) → FastAPI → Dashboard.
Deployment: GitLab CI → container registry → ECS, ALB with BLUE and GREEN target groups; Jenkins as secondary build. Locally and in CI rehearsal: two app containers behind an nginx container.
**Say:** "Settlements are aggregated to one row per transaction before anything joins them. That single decision is what the incident later broke."

## Slide 3 — Production readiness (1 min)
Tests ✓ 106/106 in six stages · Security ✓ 0 blocking (SAST triaged, secrets, SCA, container) · Docker ✓ · GitLab CI ✓ 8 stages with manual prod approval · Jenkins ✓ independent build · Blue-Green ✓ prechecks gate the cutover.
**Say:** "Every tick links to a file in the evidence package."

## Slide 4 — Production incident (1.5 min)
After cutover to v2: settlement rate 92.40% → 99.21%, ₹2.94 Cr of settlements overstated, gap shrank from ₹3.29 Cr to ₹0.34 Cr, 19 merchant views returned errors. `/health` stayed 200.
Detected by KPI baseline drift and the reconciliation gate (4 failed checks), confirmed by the artifact integrity warning and the release diff.
**Say:** "The dangerous part: 99% looks like good news. Only reconciling against the facts showed it was false."

## Slide 5 — Decision (1 min)
Rollback, not fix-forward. Blue was running, healthy and on the approved data; switching took under a minute; the fix needs a code change plus a test, which should not be written in production.

## Slide 6 — Recovery (1 min)
V2 → incident (08:00:32) → detection (08:00:34) → root cause (08:00:38) → rollback (08:00:38) → V1 (08:00:38) → smoke 11/11 and KPI reconciled at 92.40% (08:00:39).

## Slide 7 — Prevention (1 min)
1. Multi-settlement regression tests (the defect fails 12 tests).
2. Reconciliation gate: API vs facts, per merchant, vs baseline.
3. Artifact integrity: changed code cannot receive traffic.
4. Continuous KPI anomaly monitoring (open, 2026-10-09).
5. Mandatory review of Gold metric queries (open, 2026-10-02).
6. Business-KPI smoke tests instead of `/health` only.

## Slide 8 — Business outcome (1 min)
- **Settlement visibility:** one reconciled rate, gap and SLA per merchant per day.
- **Operational investigation:** exceptions list with reasons and risk as at period end.
- **Data trust:** every number recomputable from facts; quarantine explains what was excluded.
- **Deployment safety:** no traffic without tests, scans and KPI checks on the exact artifact.
- **Recovery:** rollback in under a minute, verified by business KPIs.

## Likely questions
- *Why not fix forward?* Known-good version was live and verified; the fix needed a test first.
- *Why did 99% not trip "rate > 100%"?* Healthy merchants inflated past 100% but unhealthy ones pulled the total down; we check per merchant.
- *Why Jenkins?* Continuity and legacy estate; it never deploys to production.
- *What would you do differently?* Run reconciliation continuously in production, not only at deploy time.
