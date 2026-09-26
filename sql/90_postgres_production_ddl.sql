-- Production DDL (PostgreSQL 15 / Amazon RDS or Aurora PostgreSQL).
-- The local reference implementation runs on DuckDB for zero-setup demonstration;
-- this file is the same model expressed with production physical design:
-- declarative partitioning, surrogate key sequences, enforced foreign keys and role based grants.

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS ctl;

-- ---------------------------------------------------------------- BRONZE
-- Grain: one row = one source row as received. Partitioned by ingestion day so
-- old raw partitions can be detached and archived to S3 Glacier after 90 days.
CREATE TABLE IF NOT EXISTS bronze.transactions (
    transaction_id   text,
    merchant_id      text,
    customer_id      text,
    transaction_ts   text,
    amount           text,
    currency         text,
    status           text,
    payment_channel  text,
    _source_file     text        NOT NULL,
    _source_row_num  bigint      NOT NULL,
    _row_hash        text        NOT NULL,
    _batch_id        text        NOT NULL,
    _ingested_at     timestamptz NOT NULL
) PARTITION BY RANGE (_ingested_at);

CREATE TABLE IF NOT EXISTS bronze.transactions_2026_09
    PARTITION OF bronze.transactions FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

-- ---------------------------------------------------------------- SILVER
-- Grain: one row = one payment transaction attempt.
CREATE TABLE IF NOT EXISTS silver.transactions (
    transaction_id    text          PRIMARY KEY,
    merchant_id       text          NOT NULL,
    customer_id       text          NOT NULL,
    customer_key_hash text          NOT NULL,
    transaction_ts    timestamptz   NOT NULL,
    transaction_date  date          NOT NULL,
    amount            numeric(18,2) NOT NULL CHECK (amount >= 0),
    currency          char(3)       NOT NULL CHECK (currency = 'INR'),
    status            text          NOT NULL CHECK (status IN ('SUCCESS','FAILED','REVERSED')),
    payment_channel   text          NOT NULL CHECK (payment_channel IN ('POS','ONLINE','QR')),
    is_merchant_known boolean       NOT NULL,
    _batch_id         text          NOT NULL,
    _ingested_at      timestamptz   NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_silver_txn_date ON silver.transactions (transaction_date);
CREATE INDEX IF NOT EXISTS ix_silver_txn_merchant_date ON silver.transactions (merchant_id, transaction_date);
CREATE INDEX IF NOT EXISTS ix_silver_txn_ingested ON silver.transactions (_ingested_at);

-- Grain: one row = one settlement record.
CREATE TABLE IF NOT EXISTS silver.settlements (
    settlement_id     text          PRIMARY KEY,
    transaction_id    text          NOT NULL,
    settlement_ts     timestamptz   NOT NULL,
    settlement_date   date          NOT NULL,
    settlement_amount numeric(18,2) NOT NULL,
    settlement_status text          NOT NULL CHECK (settlement_status IN ('SETTLED','PENDING','FAILED')),
    settlement_batch  text,
    is_orphan         boolean       NOT NULL,
    _batch_id         text          NOT NULL,
    _ingested_at      timestamptz   NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_silver_stl_txn ON silver.settlements (transaction_id);
CREATE INDEX IF NOT EXISTS ix_silver_stl_date ON silver.settlements (settlement_date);

-- Grain: one row = one merchant risk validity period.
CREATE TABLE IF NOT EXISTS silver.merchant (
    merchant_id       text NOT NULL,
    merchant_name     text NOT NULL,
    merchant_category text,
    country           char(2),
    risk_level        text NOT NULL CHECK (risk_level IN ('LOW','MEDIUM','HIGH')),
    effective_from    date NOT NULL,
    effective_to      date NOT NULL DEFAULT DATE '9999-12-31',
    is_current        boolean NOT NULL,
    _batch_id         text NOT NULL,
    _ingested_at      timestamptz NOT NULL,
    PRIMARY KEY (merchant_id, effective_from),
    CONSTRAINT ck_merchant_window CHECK (effective_to >= effective_from),
    EXCLUDE USING gist (
        merchant_id WITH =,
        daterange(effective_from, effective_to, '[]') WITH &&
    )
);

-- Grain: one row = one payment lifecycle event. Partitioned by business date.
CREATE TABLE IF NOT EXISTS silver.payment_events (
    event_id          text        NOT NULL,
    transaction_id    text        NOT NULL,
    event_type        text        NOT NULL CHECK (event_type IN ('CREATED','AUTHORIZED','SETTLED','FAILED')),
    event_ts          timestamptz NOT NULL,
    ingestion_ts      timestamptz NOT NULL,
    event_date        date        NOT NULL,
    processing_ms     integer,
    ingestion_lag_sec double precision NOT NULL,
    is_late_arriving  boolean     NOT NULL,
    is_sla_breach     boolean     NOT NULL,
    _batch_id         text        NOT NULL,
    _ingested_at      timestamptz NOT NULL,
    PRIMARY KEY (event_id, event_date)
) PARTITION BY RANGE (event_date);

CREATE TABLE IF NOT EXISTS silver.payment_events_2026_09
    PARTITION OF silver.payment_events FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

-- ---------------------------------------------------------------- GOLD
CREATE TABLE IF NOT EXISTS gold.dim_date (
    date_key     integer PRIMARY KEY,
    full_date    date    NOT NULL UNIQUE,
    day_of_month smallint NOT NULL,
    month_num    smallint NOT NULL,
    month_name   text     NOT NULL,
    quarter_num  smallint NOT NULL,
    year_num     smallint NOT NULL,
    day_name     text     NOT NULL,
    is_weekend   boolean  NOT NULL
);

CREATE SEQUENCE IF NOT EXISTS gold.merchant_key_seq;
CREATE TABLE IF NOT EXISTS gold.dim_merchant (
    merchant_key      bigint PRIMARY KEY DEFAULT nextval('gold.merchant_key_seq'),
    merchant_id       text NOT NULL,
    merchant_name     text NOT NULL,
    merchant_category text,
    country           char(2),
    risk_level        text NOT NULL,
    effective_from    date NOT NULL,
    effective_to      date NOT NULL,
    is_current        boolean NOT NULL,
    UNIQUE (merchant_id, effective_from)
);
CREATE INDEX IF NOT EXISTS ix_dim_merchant_lookup ON gold.dim_merchant (merchant_id, effective_from, effective_to);

CREATE SEQUENCE IF NOT EXISTS gold.customer_key_seq;
CREATE TABLE IF NOT EXISTS gold.dim_customer (
    customer_key     bigint PRIMARY KEY DEFAULT nextval('gold.customer_key_seq'),
    customer_id_hash text NOT NULL UNIQUE,
    first_seen_date  date,
    last_seen_date   date
);

-- Grain: one row = one payment transaction attempt. Partitioned by transaction_date.
CREATE TABLE IF NOT EXISTS gold.fact_transaction (
    transaction_id   text          NOT NULL,
    date_key         integer       NOT NULL REFERENCES gold.dim_date (date_key),
    merchant_key     bigint        NOT NULL REFERENCES gold.dim_merchant (merchant_key),
    customer_key     bigint        NOT NULL REFERENCES gold.dim_customer (customer_key),
    transaction_ts   timestamptz   NOT NULL,
    transaction_date date          NOT NULL,
    amount           numeric(18,2) NOT NULL,
    currency         char(3)       NOT NULL,
    status           text          NOT NULL,
    payment_channel  text          NOT NULL,
    is_successful    boolean       NOT NULL,
    _batch_id        text          NOT NULL,
    _loaded_at       timestamptz   NOT NULL,
    PRIMARY KEY (transaction_id, transaction_date)
) PARTITION BY RANGE (transaction_date);

CREATE TABLE IF NOT EXISTS gold.fact_transaction_2026_09
    PARTITION OF gold.fact_transaction FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE INDEX IF NOT EXISTS ix_fact_txn_merchant ON gold.fact_transaction (merchant_key, transaction_date);

-- Grain: one row = one settlement record.
CREATE TABLE IF NOT EXISTS gold.fact_settlement (
    settlement_id     text          NOT NULL,
    transaction_id    text          NOT NULL,
    date_key          integer       NOT NULL REFERENCES gold.dim_date (date_key),
    merchant_key      bigint        REFERENCES gold.dim_merchant (merchant_key),
    settlement_ts     timestamptz   NOT NULL,
    settlement_date   date          NOT NULL,
    settlement_amount numeric(18,2) NOT NULL,
    settlement_status text          NOT NULL,
    settlement_batch  text,
    is_orphan         boolean       NOT NULL,
    _batch_id         text          NOT NULL,
    _loaded_at        timestamptz   NOT NULL,
    PRIMARY KEY (settlement_id, settlement_date)
) PARTITION BY RANGE (settlement_date);

CREATE TABLE IF NOT EXISTS gold.fact_settlement_2026_09
    PARTITION OF gold.fact_settlement FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');
CREATE INDEX IF NOT EXISTS ix_fact_stl_txn ON gold.fact_settlement (transaction_id);

-- Grain: one row = one SUCCESSFUL transaction with its settlements pre-aggregated.
CREATE TABLE IF NOT EXISTS gold.fact_settlement_reconciliation (
    transaction_id     text          NOT NULL,
    date_key           integer       NOT NULL REFERENCES gold.dim_date (date_key),
    merchant_key       bigint        NOT NULL REFERENCES gold.dim_merchant (merchant_key),
    transaction_ts     timestamptz   NOT NULL,
    transaction_date   date          NOT NULL,
    transaction_amount numeric(18,2) NOT NULL,
    settled_amount     numeric(18,2) NOT NULL,
    pending_amount     numeric(18,2) NOT NULL,
    failed_amount      numeric(18,2) NOT NULL,
    settlement_gap     numeric(18,2) NOT NULL,
    settlement_count   integer       NOT NULL,
    first_settled_ts   timestamptz,
    minutes_to_settle  double precision,
    settlement_state   text          NOT NULL CHECK (settlement_state IN
                         ('FULLY_SETTLED','PARTIALLY_SETTLED','PENDING','SETTLEMENT_FAILED','UNSETTLED')),
    is_sla_met         boolean,
    _batch_id          text          NOT NULL,
    _loaded_at         timestamptz   NOT NULL,
    PRIMARY KEY (transaction_id, transaction_date)
) PARTITION BY RANGE (transaction_date);

CREATE TABLE IF NOT EXISTS gold.fact_settlement_reconciliation_2026_09
    PARTITION OF gold.fact_settlement_reconciliation FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

-- Grain: one row = one transaction_date x merchant_key.
CREATE TABLE IF NOT EXISTS gold.agg_daily_merchant_settlement (
    transaction_date   date          NOT NULL,
    merchant_key       bigint        NOT NULL REFERENCES gold.dim_merchant (merchant_key),
    merchant_id        text          NOT NULL,
    merchant_name      text          NOT NULL,
    risk_level         text          NOT NULL,
    transaction_count  bigint        NOT NULL,
    success_count      bigint        NOT NULL,
    transaction_amount numeric(18,2) NOT NULL,
    settled_amount     numeric(18,2) NOT NULL,
    settlement_gap     numeric(18,2) NOT NULL,
    unsettled_count    bigint        NOT NULL,
    sla_eligible_count bigint        NOT NULL,
    sla_met_count      bigint        NOT NULL,
    settlement_rate    double precision NOT NULL CHECK (settlement_rate BETWEEN 0 AND 100.001),
    sla_rate           double precision NOT NULL CHECK (sla_rate BETWEEN 0 AND 100.001),
    _loaded_at         timestamptz   NOT NULL,
    PRIMARY KEY (transaction_date, merchant_key)
);
CREATE INDEX IF NOT EXISTS ix_agg_merchant ON gold.agg_daily_merchant_settlement (merchant_id, transaction_date);

-- ---------------------------------------------------------------- CONTROL
CREATE TABLE IF NOT EXISTS ctl.etl_watermark (
    target_table     text PRIMARY KEY,
    last_ingested_at timestamptz NOT NULL,
    last_batch_id    text,
    rows_processed   bigint NOT NULL DEFAULT 0,
    updated_at       timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS ctl.dq_quarantine (
    quarantine_id  text PRIMARY KEY,
    source_table   text NOT NULL,
    business_key   text,
    rule_id        text NOT NULL,
    severity       text NOT NULL,
    disposition    text NOT NULL CHECK (disposition IN ('REJECT','QUARANTINE','WARN','BUSINESS_EXCEPTION')),
    reason         text NOT NULL,
    record_payload jsonb NOT NULL,
    batch_id       text NOT NULL,
    detected_at    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quarantine_rule ON ctl.dq_quarantine (rule_id, detected_at);

-- ---------------------------------------------------------------- SECURITY
-- Least privilege: the API role may only read the serving objects it needs.
CREATE ROLE settlement_api_ro NOLOGIN;
GRANT USAGE ON SCHEMA gold, ctl TO settlement_api_ro;
GRANT SELECT ON gold.agg_daily_merchant_settlement, gold.dim_merchant, gold.dim_date TO settlement_api_ro;
GRANT SELECT ON ctl.dq_rule_result TO settlement_api_ro;
REVOKE ALL ON SCHEMA silver FROM settlement_api_ro;

CREATE ROLE settlement_etl_rw NOLOGIN;
GRANT USAGE ON SCHEMA bronze, silver, gold, ctl TO settlement_etl_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA bronze, silver, gold, ctl TO settlement_etl_rw;
