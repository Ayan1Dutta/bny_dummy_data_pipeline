from __future__ import annotations

from dataclasses import dataclass

REJECT = "REJECT"
QUARANTINE = "QUARANTINE"
WARN = "WARN"
BUSINESS_EXCEPTION = "BUSINESS_EXCEPTION"

BLOCKING = {REJECT, QUARANTINE}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    source_table: str
    severity: str
    disposition: str
    description: str
    predicate: str


RULES: tuple[Rule, ...] = (
    Rule("TXN-001", "transactions", "CRITICAL", REJECT,
         "transaction_id is null or blank - record cannot be keyed",
         "transaction_id IS NULL OR trim(transaction_id) = ''"),
    Rule("TXN-002", "transactions", "CRITICAL", REJECT,
         "transaction_ts is not a parsable timestamp",
         "ts_typed IS NULL"),
    Rule("TXN-003", "transactions", "CRITICAL", REJECT,
         "amount is not numeric",
         "amount_typed IS NULL"),
    Rule("TXN-004", "transactions", "CRITICAL", REJECT,
         "amount is negative - not a valid payment attempt",
         "amount_typed < 0"),
    Rule("TXN-005", "transactions", "HIGH", QUARANTINE,
         "merchant_id missing - transaction cannot be attributed or settled to a merchant",
         "merchant_id IS NULL OR trim(merchant_id) = ''"),
    Rule("TXN-006", "transactions", "HIGH", QUARANTINE,
         "currency not in the allowed list (INR)",
         "currency IS NULL OR upper(trim(currency)) NOT IN ('INR')"),
    Rule("TXN-007", "transactions", "HIGH", QUARANTINE,
         "status outside SUCCESS / FAILED / REVERSED",
         "upper(trim(coalesce(status,''))) NOT IN ('SUCCESS','FAILED','REVERSED')"),
    Rule("TXN-008", "transactions", "MEDIUM", QUARANTINE,
         "payment_channel outside POS / ONLINE / QR",
         "upper(trim(coalesce(payment_channel,''))) NOT IN ('POS','ONLINE','QR')"),
    Rule("TXN-009", "transactions", "MEDIUM", WARN,
         "duplicate transaction_id in the source batch - latest ingestion wins",
         "dup_rank > 1"),
    Rule("TXN-010", "transactions", "MEDIUM", WARN,
         "merchant_id not present in the merchant master - mapped to UNKNOWN merchant key",
         "is_merchant_known = FALSE"),

    Rule("STL-001", "settlements", "CRITICAL", REJECT,
         "settlement_id is null or blank",
         "settlement_id IS NULL OR trim(settlement_id) = ''"),
    Rule("STL-002", "settlements", "CRITICAL", REJECT,
         "settlement_ts is not a parsable timestamp",
         "ts_typed IS NULL"),
    Rule("STL-003", "settlements", "CRITICAL", REJECT,
         "settlement_amount is not numeric",
         "amount_typed IS NULL"),
    Rule("STL-004", "settlements", "HIGH", QUARANTINE,
         "negative settlement amount - requires operations confirmation before it affects money KPIs",
         "amount_typed < 0"),
    Rule("STL-005", "settlements", "HIGH", QUARANTINE,
         "settlement_status outside SETTLED / PENDING / FAILED",
         "upper(trim(coalesce(settlement_status,''))) NOT IN ('SETTLED','PENDING','FAILED')"),
    Rule("STL-006", "settlements", "HIGH", BUSINESS_EXCEPTION,
         "settlement has no matching transaction - loaded, flagged as orphan, excluded from settlement rate",
         "is_orphan = TRUE"),
    Rule("STL-007", "settlements", "MEDIUM", WARN,
         "duplicate settlement_id - latest ingestion wins",
         "dup_rank > 1"),
    Rule("STL-008", "settlements", "MEDIUM", BUSINESS_EXCEPTION,
         "settled amount exceeds the transaction amount - over-settlement",
         "over_settled = TRUE"),

    Rule("EVT-001", "payment_events", "CRITICAL", REJECT,
         "event_id is null or blank",
         "event_id IS NULL OR trim(event_id) = ''"),
    Rule("EVT-002", "payment_events", "CRITICAL", REJECT,
         "event_ts or ingestion_ts is not a parsable timestamp",
         "event_ts_typed IS NULL OR ingestion_ts_typed IS NULL"),
    Rule("EVT-003", "payment_events", "HIGH", QUARANTINE,
         "event_type outside CREATED / AUTHORIZED / SETTLED / FAILED",
         "upper(trim(coalesce(event_type,''))) NOT IN ('CREATED','AUTHORIZED','SETTLED','FAILED')"),
    Rule("EVT-004", "payment_events", "MEDIUM", WARN,
         "duplicate event_id - earliest ingestion wins, later copies discarded",
         "dup_rank > 1"),
    Rule("EVT-005", "payment_events", "LOW", WARN,
         "late arriving event - ingestion_ts more than 5 minutes after event_ts",
         "is_late_arriving = TRUE"),
    Rule("EVT-006", "payment_events", "MEDIUM", BUSINESS_EXCEPTION,
         "ingestion SLA breach - event received more than 15 minutes after it occurred",
         "is_sla_breach = TRUE"),
    Rule("EVT-007", "payment_events", "LOW", WARN,
         "ingestion_ts earlier than event_ts - clock skew between producer and platform",
         "ingestion_lag_sec < 0"),

    Rule("MER-001", "merchant", "CRITICAL", REJECT,
         "merchant_id or effective_from missing",
         "merchant_id IS NULL OR trim(merchant_id) = '' OR eff_from_typed IS NULL"),
    Rule("MER-002", "merchant", "HIGH", QUARANTINE,
         "risk_level outside LOW / MEDIUM / HIGH",
         "upper(trim(coalesce(risk_level,''))) NOT IN ('LOW','MEDIUM','HIGH')"),
    Rule("MER-003", "merchant", "HIGH", QUARANTINE,
         "effective_to earlier than effective_from - invalid validity window",
         "eff_to_typed < eff_from_typed"),
    Rule("MER-004", "merchant", "HIGH", BUSINESS_EXCEPTION,
         "overlapping risk validity windows for the same merchant",
         "has_overlap = TRUE"),
)

BY_TABLE: dict[str, tuple[Rule, ...]] = {
    table: tuple(r for r in RULES if r.source_table == table)
    for table in {r.source_table for r in RULES}
}


def blocking_rules(table: str) -> tuple[Rule, ...]:
    return tuple(r for r in BY_TABLE[table] if r.disposition in BLOCKING)


def non_blocking_rules(table: str) -> tuple[Rule, ...]:
    return tuple(r for r in BY_TABLE[table] if r.disposition not in BLOCKING)


def rule(rule_id: str) -> Rule:
    for r in RULES:
        if r.rule_id == rule_id:
            return r
    raise KeyError(rule_id)
