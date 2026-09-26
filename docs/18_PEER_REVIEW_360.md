# 18 — 360° Peer Review

Reviewer: ______ Reviewed team: ______ Date: ______

## Scores (1–5)

| Area | 1–2 | 3 | 4–5 | Score | Evidence seen |
|---|---|---|---|---|---|
| Technical implementation | Major gaps | Functional | Strong | | |
| Data modelling | Significant issues | Adequate | Well justified | | |
| Testing | Limited | Reasonable | Comprehensive | | |
| Security | Major gaps | Basic controls | Strong controls | | |
| CI/CD | Incomplete | Working | Well engineered | | |
| Incident response | Unclear | Adequate | Evidence-driven | | |
| Communication | Difficult to follow | Clear | Structured | | |
| Business understanding | Weak | Good | Strong | | |
| Team collaboration | Limited | Good | Strong | | |
| **Total (/45)** | | | | | |

## What to check before scoring
- Testing: is there a negative test for one transaction with multiple settlements, and does it prove the trap is real?
- Security: are SAST findings triaged with reasons, and does SCA fail closed?
- CI/CD: is prod behind manual approval and protected variables? Is there a rollback job?
- Incident: did they compare against an independent source, not just the previous API output? Before/after numbers?
- Decision: rollback vs fix-forward justified by evidence, not preference?

## Questions
1. What was technically strong?
2. What was the biggest engineering risk?
3. Was the production decision supported by evidence?
4. Was the rollback strategy adequate?
5. What one improvement would you recommend?

## Self-review (our team)

| Area | Score | Reason |
|---|---|---|
| Technical implementation | 5 | Full pipeline, API, blue-green controller and gates working end to end |
| Data modelling | 5 | Transaction grain, settlement pre-aggregation, SCD2 merchant, reconciliation fact |
| Testing | 5 | 106 tests in 6 stages; gates tested against the failures they must catch |
| Security | 4 | Four scans, fail-closed SCA, prod secret guard; container scan only in CI |
| CI/CD | 4 | Complete GitLab and Jenkins definitions; not executed against real GitLab/AWS here |
| Incident response | 5 | Detection, diagnosis, rollback and validation all backed by logged evidence |
| Communication | 4 | Structured docs and slides |
| Business understanding | 5 | Impact stated in rupees and operational effect |
| Team collaboration | — | Assessed by peers |

Biggest risk: reconciliation runs at deploy and rollback, not continuously. Recommended improvement: schedule the gate against production and alert on FAIL.
