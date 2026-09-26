-- Layer: GOLD (dimensional model + serving aggregates consumed by the API)

CREATE SCHEMA IF NOT EXISTS gold;

-- Grain: one row = one calendar day.
CREATE TABLE IF NOT EXISTS gold.dim_date (
    date_key      INTEGER NOT NULL PRIMARY KEY,
    full_date     DATE    NOT NULL,
    day_of_month  INTEGER NOT NULL,
    month_num     INTEGER NOT NULL,
    month_name    VARCHAR NOT NULL,
    quarter_num   INTEGER NOT NULL,
    year_num      INTEGER NOT NULL,
    day_name      VARCHAR NOT NULL,
    is_weekend    BOOLEAN NOT NULL
);

-- Grain: one row = one merchant risk validity period (SCD2). merchant_key is the surrogate key.
CREATE TABLE IF NOT EXISTS gold.dim_merchant (
    merchant_key      BIGINT  NOT NULL PRIMARY KEY,
    merchant_id       VARCHAR NOT NULL,
    merchant_name     VARCHAR NOT NULL,
    merchant_category VARCHAR,
    country           VARCHAR,
    risk_level        VARCHAR NOT NULL,
    effective_from    DATE    NOT NULL,
    effective_to      DATE    NOT NULL,
    is_current        BOOLEAN NOT NULL
);

-- Grain: one row = one customer (pseudonymised; raw customer_id never leaves silver).
CREATE TABLE IF NOT EXISTS gold.dim_customer (
    customer_key      BIGINT  NOT NULL PRIMARY KEY,
    customer_id_hash  VARCHAR NOT NULL,
    first_seen_date   DATE,
    last_seen_date    DATE
);

-- Grain: one row = one payment transaction attempt.
CREATE TABLE IF NOT EXISTS gold.fact_transaction (
    transaction_id   VARCHAR       NOT NULL PRIMARY KEY,
    date_key         INTEGER       NOT NULL REFERENCES gold.dim_date(date_key),
    merchant_key     BIGINT        NOT NULL REFERENCES gold.dim_merchant(merchant_key),
    customer_key     BIGINT        NOT NULL REFERENCES gold.dim_customer(customer_key),
    transaction_ts   TIMESTAMP     NOT NULL,
    transaction_date DATE          NOT NULL,
    amount           DECIMAL(18,2) NOT NULL,
    currency         VARCHAR       NOT NULL,
    status           VARCHAR       NOT NULL,
    payment_channel  VARCHAR       NOT NULL,
    is_successful    BOOLEAN       NOT NULL,
    _batch_id        VARCHAR       NOT NULL,
    _loaded_at       TIMESTAMP     NOT NULL
);

-- Grain: one row = one settlement record (never joined straight to fact_transaction for money maths).
CREATE TABLE IF NOT EXISTS gold.fact_settlement (
    settlement_id     VARCHAR       NOT NULL PRIMARY KEY,
    transaction_id    VARCHAR       NOT NULL,
    date_key          INTEGER       NOT NULL REFERENCES gold.dim_date(date_key),
    merchant_key      BIGINT,
    settlement_ts     TIMESTAMP     NOT NULL,
    settlement_date   DATE          NOT NULL,
    settlement_amount DECIMAL(18,2) NOT NULL,
    settlement_status VARCHAR       NOT NULL,
    settlement_batch  VARCHAR,
    is_orphan         BOOLEAN       NOT NULL,
    _batch_id         VARCHAR       NOT NULL,
    _loaded_at        TIMESTAMP     NOT NULL
);

-- Grain: one row = one payment lifecycle event.
CREATE TABLE IF NOT EXISTS gold.fact_payment_event (
    event_id          VARCHAR   NOT NULL PRIMARY KEY,
    transaction_id    VARCHAR   NOT NULL,
    date_key          INTEGER   NOT NULL REFERENCES gold.dim_date(date_key),
    event_type        VARCHAR   NOT NULL,
    event_ts          TIMESTAMP NOT NULL,
    ingestion_ts      TIMESTAMP NOT NULL,
    processing_ms     INTEGER,
    ingestion_lag_sec DOUBLE    NOT NULL,
    is_late_arriving  BOOLEAN   NOT NULL,
    is_sla_breach     BOOLEAN   NOT NULL,
    _batch_id         VARCHAR   NOT NULL,
    _loaded_at        TIMESTAMP NOT NULL
);

-- Grain: one row = one SUCCESSFUL transaction, with its settlements pre-aggregated.
-- This table is the fan-out guard: settlements are collapsed to transaction level here, once.
CREATE TABLE IF NOT EXISTS gold.fact_settlement_reconciliation (
    transaction_id     VARCHAR       NOT NULL PRIMARY KEY,
    date_key           INTEGER       NOT NULL REFERENCES gold.dim_date(date_key),
    merchant_key       BIGINT        NOT NULL REFERENCES gold.dim_merchant(merchant_key),
    transaction_ts     TIMESTAMP     NOT NULL,
    transaction_date   DATE          NOT NULL,
    transaction_amount DECIMAL(18,2) NOT NULL,
    settled_amount     DECIMAL(18,2) NOT NULL,
    pending_amount     DECIMAL(18,2) NOT NULL,
    failed_amount      DECIMAL(18,2) NOT NULL,
    settlement_gap     DECIMAL(18,2) NOT NULL,
    settlement_count   INTEGER       NOT NULL,
    first_settled_ts   TIMESTAMP,
    minutes_to_settle  DOUBLE,
    settlement_state   VARCHAR       NOT NULL,
    is_sla_met         BOOLEAN,
    _batch_id          VARCHAR       NOT NULL,
    _loaded_at         TIMESTAMP     NOT NULL
);

-- Grain: one row = one transaction_date x merchant_key. Serving table behind the API.
CREATE TABLE IF NOT EXISTS gold.agg_daily_merchant_settlement (
    transaction_date     DATE          NOT NULL,
    merchant_key         BIGINT        NOT NULL,
    merchant_id          VARCHAR       NOT NULL,
    merchant_name        VARCHAR       NOT NULL,
    risk_level           VARCHAR       NOT NULL,
    transaction_count    BIGINT        NOT NULL,
    success_count        BIGINT        NOT NULL,
    transaction_amount   DECIMAL(18,2) NOT NULL,
    settled_amount       DECIMAL(18,2) NOT NULL,
    settlement_gap       DECIMAL(18,2) NOT NULL,
    unsettled_count      BIGINT        NOT NULL,
    sla_eligible_count   BIGINT        NOT NULL,
    sla_met_count        BIGINT        NOT NULL,
    settlement_rate      DOUBLE        NOT NULL,
    sla_rate             DOUBLE        NOT NULL,
    _loaded_at           TIMESTAMP     NOT NULL,
    PRIMARY KEY (transaction_date, merchant_key)
);

CREATE INDEX IF NOT EXISTS ix_fact_txn_date      ON gold.fact_transaction(transaction_date);
CREATE INDEX IF NOT EXISTS ix_fact_txn_merchant  ON gold.fact_transaction(merchant_key);
CREATE INDEX IF NOT EXISTS ix_fact_stl_txn       ON gold.fact_settlement(transaction_id);
CREATE INDEX IF NOT EXISTS ix_recon_date         ON gold.fact_settlement_reconciliation(transaction_date);
CREATE INDEX IF NOT EXISTS ix_agg_date           ON gold.agg_daily_merchant_settlement(transaction_date);
