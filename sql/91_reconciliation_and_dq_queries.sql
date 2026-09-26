-- Query pack used for the engineering review, the data model tests and the daily operations check.
-- Every query below is safe to run read-only against the gold and control schemas.

-- 1. GRAIN: fact_transaction must hold exactly one row per transaction attempt. Expected: 0 rows.
SELECT transaction_id, count(*)
FROM gold.fact_transaction
GROUP BY transaction_id
HAVING count(*) > 1;

-- 2. GRAIN: fact_settlement must hold exactly one row per settlement record. Expected: 0 rows.
SELECT settlement_id, count(*)
FROM gold.fact_settlement
GROUP BY settlement_id
HAVING count(*) > 1;

-- 3. GRAIN: the reconciliation fact must hold exactly one row per successful transaction. Expected: 0 rows.
SELECT transaction_id, count(*)
FROM gold.fact_settlement_reconciliation
GROUP BY transaction_id
HAVING count(*) > 1;

-- 4. REFERENTIAL INTEGRITY: no transaction may reference a merchant that does not exist. Expected: 0 rows.
SELECT f.transaction_id, f.merchant_key
FROM gold.fact_transaction f
LEFT JOIN gold.dim_merchant m ON m.merchant_key = f.merchant_key
WHERE m.merchant_key IS NULL;

-- 5. REFERENTIAL INTEGRITY: settlements that reference a missing transaction must be flagged orphan.
SELECT s.settlement_id, s.transaction_id, s.is_orphan
FROM gold.fact_settlement s
LEFT JOIN gold.fact_transaction t ON t.transaction_id = s.transaction_id
WHERE t.transaction_id IS NULL AND NOT s.is_orphan;

-- 6. SCD2: merchant risk windows must never overlap. Expected: 0 rows.
SELECT a.merchant_id, a.effective_from, a.effective_to, b.effective_from, b.effective_to
FROM gold.dim_merchant a
JOIN gold.dim_merchant b
  ON a.merchant_id = b.merchant_id AND a.effective_from < b.effective_from
WHERE b.effective_from <= a.effective_to;

-- 7. THE FAN-OUT TRAP: this is the WRONG way to compute settlement value.
--    A direct join multiplies the transaction amount by the number of settlement rows.
SELECT sum(t.amount) AS inflated_transaction_amount
FROM gold.fact_transaction t
JOIN gold.fact_settlement s ON s.transaction_id = t.transaction_id
WHERE t.is_successful;

-- 8. THE CORRECT WAY: aggregate settlements to transaction grain first, then join once.
SELECT sum(t.amount) AS transaction_amount, sum(coalesce(s.settled_amount, 0)) AS settled_amount
FROM gold.fact_transaction t
LEFT JOIN (
    SELECT transaction_id, sum(settlement_amount) AS settled_amount
    FROM gold.fact_settlement
    WHERE settlement_status = 'SETTLED' AND NOT is_orphan
    GROUP BY transaction_id
) s ON s.transaction_id = t.transaction_id
WHERE t.is_successful;

-- 9. SETTLEMENT RECONCILIATION: the four-way tie-out operations asks for every morning.
SELECT
    sum(transaction_amount)                                                  AS successful_transaction_value,
    sum(settled_amount)                                                      AS settled_value,
    sum(pending_amount)                                                      AS pending_value,
    sum(failed_amount)                                                       AS failed_settlement_value,
    sum(transaction_amount) - sum(settled_amount)                            AS settlement_gap,
    count(*) FILTER (WHERE settlement_state = 'UNSETTLED')                   AS unsettled_transactions,
    count(*) FILTER (WHERE settlement_state = 'PARTIALLY_SETTLED')           AS partially_settled_transactions,
    count(*) FILTER (WHERE settlement_state = 'PENDING')                     AS pending_transactions,
    count(*) FILTER (WHERE settlement_state = 'SETTLEMENT_FAILED')           AS failed_transactions
FROM gold.fact_settlement_reconciliation
WHERE transaction_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-07';

-- 10. GAP DECOMPOSITION: answers "why is the gap what it is" in one result set.
SELECT settlement_state,
       count(*)                                        AS transactions,
       sum(transaction_amount)                         AS transaction_value,
       sum(settlement_gap)                             AS gap_contribution,
       round(100.0 * sum(settlement_gap) /
             nullif(sum(sum(settlement_gap)) OVER (), 0), 2) AS pct_of_total_gap
FROM gold.fact_settlement_reconciliation
WHERE transaction_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-07'
GROUP BY settlement_state
ORDER BY gap_contribution DESC;

-- 11. MONEY COMPLETENESS: source value must equal loaded value plus quarantined value.
SELECT
    (SELECT count(*) FROM bronze.transactions)                                              AS source_rows,
    (SELECT count(*) FROM silver.transactions)                                              AS loaded_rows,
    (SELECT count(DISTINCT business_key) FROM ctl.dq_quarantine
      WHERE source_table = 'transactions' AND disposition IN ('REJECT','QUARANTINE'))       AS quarantined_rows,
    (SELECT coalesce(sum(failed_rows), 0) FROM ctl.dq_rule_result WHERE rule_id = 'TXN-009') AS deduplicated_rows;

-- 12. KPI 5: merchants breaching both operational thresholds.
SELECT merchant_id,
       max(merchant_name)                                                                  AS merchant_name,
       round(100.0 * sum(settled_amount) / nullif(sum(transaction_amount), 0), 2)          AS settlement_rate,
       round(100.0 * sum(sla_met_count) / nullif(sum(sla_eligible_count), 0), 2)           AS sla_rate,
       sum(settlement_gap)                                                                 AS settlement_gap
FROM gold.agg_daily_merchant_settlement
WHERE transaction_date BETWEEN DATE '2026-09-01' AND DATE '2026-09-07'
GROUP BY merchant_id
HAVING round(100.0 * sum(settled_amount) / nullif(sum(transaction_amount), 0), 2) < 95
   AND round(100.0 * sum(sla_met_count) / nullif(sum(sla_eligible_count), 0), 2) < 90
ORDER BY settlement_gap DESC;

-- 13. LATE ARRIVAL PROFILE: how far behind the platform is receiving events.
SELECT event_date,
       count(*)                                                   AS events,
       count(*) FILTER (WHERE is_late_arriving)                   AS late_events,
       count(*) FILTER (WHERE is_sla_breach)                      AS sla_breaches,
       round(avg(ingestion_lag_sec), 1)                           AS avg_lag_sec,
       max(ingestion_lag_sec)                                     AS max_lag_sec
FROM gold.fact_payment_event
GROUP BY event_date
ORDER BY event_date;

-- 14. QUARANTINE REVIEW QUEUE: what operations must fix, newest first.
SELECT rule_id, severity, disposition, count(*) AS records, max(detected_at) AS last_seen
FROM ctl.dq_quarantine
GROUP BY rule_id, severity, disposition
ORDER BY records DESC;

-- 15. PIPELINE HEALTH: last ten runs and what each one moved.
SELECT batch_id, run_mode, status, started_at, finished_at,
       bronze_rows, silver_rows, quarantined_rows, gold_rows
FROM ctl.pipeline_run
ORDER BY started_at DESC
LIMIT 10;
