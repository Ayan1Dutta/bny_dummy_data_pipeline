-- Layer: BRONZE (raw landing, schema-on-read, nothing rejected, everything typed as VARCHAR)
-- Grain: one row = one physical source row as received, per file, per load batch.

CREATE SCHEMA IF NOT EXISTS bronze;

CREATE TABLE IF NOT EXISTS bronze.transactions (
    transaction_id    VARCHAR,
    merchant_id       VARCHAR,
    customer_id       VARCHAR,
    transaction_ts    VARCHAR,
    amount            VARCHAR,
    currency          VARCHAR,
    status            VARCHAR,
    payment_channel   VARCHAR,
    _source_file      VARCHAR NOT NULL,
    _source_row_num   BIGINT  NOT NULL,
    _row_hash         VARCHAR NOT NULL,
    _batch_id         VARCHAR NOT NULL,
    _ingested_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.settlements (
    settlement_id     VARCHAR,
    transaction_id    VARCHAR,
    settlement_ts     VARCHAR,
    settlement_amount VARCHAR,
    settlement_status VARCHAR,
    settlement_batch  VARCHAR,
    _source_file      VARCHAR NOT NULL,
    _source_row_num   BIGINT  NOT NULL,
    _row_hash         VARCHAR NOT NULL,
    _batch_id         VARCHAR NOT NULL,
    _ingested_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.merchant (
    merchant_id       VARCHAR,
    merchant_name     VARCHAR,
    merchant_category VARCHAR,
    country           VARCHAR,
    risk_level        VARCHAR,
    effective_from    VARCHAR,
    effective_to      VARCHAR,
    _source_file      VARCHAR NOT NULL,
    _source_row_num   BIGINT  NOT NULL,
    _row_hash         VARCHAR NOT NULL,
    _batch_id         VARCHAR NOT NULL,
    _ingested_at      TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS bronze.payment_events (
    event_id          VARCHAR,
    transaction_id    VARCHAR,
    event_type        VARCHAR,
    event_ts          VARCHAR,
    ingestion_ts      VARCHAR,
    processing_ms     VARCHAR,
    _source_file      VARCHAR NOT NULL,
    _source_row_num   BIGINT  NOT NULL,
    _row_hash         VARCHAR NOT NULL,
    _batch_id         VARCHAR NOT NULL,
    _ingested_at      TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_bronze_txn_batch ON bronze.transactions(_batch_id);
CREATE INDEX IF NOT EXISTS ix_bronze_stl_batch ON bronze.settlements(_batch_id);
CREATE INDEX IF NOT EXISTS ix_bronze_evt_batch ON bronze.payment_events(_batch_id);
