-- Layer: SILVER (typed, cleansed, deduplicated, business-key enforced)
-- Only records that pass hard validation land here. Everything else lands in ctl.dq_quarantine.

CREATE SCHEMA IF NOT EXISTS silver;

-- Grain: one row = one payment transaction attempt (transaction_id is unique).
CREATE TABLE IF NOT EXISTS silver.transactions (
    transaction_id    VARCHAR       NOT NULL PRIMARY KEY,
    merchant_id       VARCHAR,
    customer_id       VARCHAR,
    customer_key_hash VARCHAR       NOT NULL,
    transaction_ts    TIMESTAMP     NOT NULL,
    transaction_date  DATE          NOT NULL,
    amount            DECIMAL(18,2) NOT NULL CHECK (amount >= 0),
    currency          VARCHAR       NOT NULL CHECK (currency IN ('INR')),
    status            VARCHAR       NOT NULL CHECK (status IN ('SUCCESS','FAILED','REVERSED')),
    payment_channel   VARCHAR       NOT NULL CHECK (payment_channel IN ('POS','ONLINE','QR')),
    is_merchant_known BOOLEAN       NOT NULL,
    _batch_id         VARCHAR       NOT NULL,
    _ingested_at      TIMESTAMP     NOT NULL
);

-- Grain: one row = one settlement record. A transaction may have many (1:N is expected, not an error).
CREATE TABLE IF NOT EXISTS silver.settlements (
    settlement_id     VARCHAR       NOT NULL PRIMARY KEY,
    transaction_id    VARCHAR       NOT NULL,
    settlement_ts     TIMESTAMP     NOT NULL,
    settlement_date   DATE          NOT NULL,
    settlement_amount DECIMAL(18,2) NOT NULL,
    settlement_status VARCHAR       NOT NULL CHECK (settlement_status IN ('SETTLED','PENDING','FAILED')),
    settlement_batch  VARCHAR,
    is_orphan         BOOLEAN       NOT NULL,
    _batch_id         VARCHAR       NOT NULL,
    _ingested_at      TIMESTAMP     NOT NULL
);

-- Grain: one row = one merchant risk validity period (SCD Type 2 natural history from source).
CREATE TABLE IF NOT EXISTS silver.merchant (
    merchant_id       VARCHAR NOT NULL,
    merchant_name     VARCHAR NOT NULL,
    merchant_category VARCHAR,
    country           VARCHAR,
    risk_level        VARCHAR NOT NULL CHECK (risk_level IN ('LOW','MEDIUM','HIGH')),
    effective_from    DATE    NOT NULL,
    effective_to      DATE    NOT NULL,
    is_current        BOOLEAN NOT NULL,
    _batch_id         VARCHAR NOT NULL,
    _ingested_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (merchant_id, effective_from)
);

-- Grain: one row = one payment lifecycle event (event_id deduplicated, keeping earliest ingestion).
CREATE TABLE IF NOT EXISTS silver.payment_events (
    event_id          VARCHAR   NOT NULL PRIMARY KEY,
    transaction_id    VARCHAR   NOT NULL,
    event_type        VARCHAR   NOT NULL CHECK (event_type IN ('CREATED','AUTHORIZED','SETTLED','FAILED')),
    event_ts          TIMESTAMP NOT NULL,
    ingestion_ts      TIMESTAMP NOT NULL,
    event_date        DATE      NOT NULL,
    processing_ms     INTEGER,
    ingestion_lag_sec DOUBLE    NOT NULL,
    is_late_arriving  BOOLEAN   NOT NULL,
    is_sla_breach     BOOLEAN   NOT NULL,
    _batch_id         VARCHAR   NOT NULL,
    _ingested_at      TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_silver_txn_date     ON silver.transactions(transaction_date);
CREATE INDEX IF NOT EXISTS ix_silver_txn_merchant ON silver.transactions(merchant_id);
CREATE INDEX IF NOT EXISTS ix_silver_stl_txn      ON silver.settlements(transaction_id);
CREATE INDEX IF NOT EXISTS ix_silver_evt_txn      ON silver.payment_events(transaction_id);
