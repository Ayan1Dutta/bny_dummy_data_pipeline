-- Layer: CONTROL (orchestration metadata, data quality audit, quarantine)

CREATE SCHEMA IF NOT EXISTS ctl;

-- Grain: one row = one target table. Drives incremental processing.
CREATE TABLE IF NOT EXISTS ctl.etl_watermark (
    target_table      VARCHAR   NOT NULL PRIMARY KEY,
    last_ingested_at  TIMESTAMP NOT NULL,
    last_batch_id     VARCHAR,
    rows_processed    BIGINT    NOT NULL DEFAULT 0,
    updated_at        TIMESTAMP NOT NULL
);

-- Grain: one row = one file successfully landed in bronze (replay / idempotency guard).
CREATE TABLE IF NOT EXISTS ctl.load_file_audit (
    source_file   VARCHAR   NOT NULL,
    batch_id      VARCHAR   NOT NULL,
    target_table  VARCHAR   NOT NULL,
    file_hash     VARCHAR   NOT NULL,
    row_count     BIGINT    NOT NULL,
    loaded_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (source_file, target_table)
);

-- Grain: one row = one rejected or quarantined source record.
CREATE TABLE IF NOT EXISTS ctl.dq_quarantine (
    quarantine_id  VARCHAR   NOT NULL PRIMARY KEY,
    source_table   VARCHAR   NOT NULL,
    business_key   VARCHAR,
    rule_id        VARCHAR   NOT NULL,
    severity       VARCHAR   NOT NULL,
    disposition    VARCHAR   NOT NULL,
    reason         VARCHAR   NOT NULL,
    record_payload VARCHAR   NOT NULL,
    batch_id       VARCHAR   NOT NULL,
    detected_at    TIMESTAMP NOT NULL
);

-- Grain: one row = one rule evaluated in one batch.
CREATE TABLE IF NOT EXISTS ctl.dq_rule_result (
    batch_id     VARCHAR   NOT NULL,
    rule_id      VARCHAR   NOT NULL,
    source_table VARCHAR   NOT NULL,
    severity     VARCHAR   NOT NULL,
    disposition  VARCHAR   NOT NULL,
    failed_rows  BIGINT    NOT NULL,
    total_rows   BIGINT    NOT NULL,
    evaluated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (batch_id, rule_id, source_table)
);

-- Grain: one row = one pipeline run.
CREATE TABLE IF NOT EXISTS ctl.pipeline_run (
    batch_id        VARCHAR   NOT NULL PRIMARY KEY,
    run_mode        VARCHAR   NOT NULL,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    status          VARCHAR   NOT NULL,
    bronze_rows     BIGINT    NOT NULL DEFAULT 0,
    silver_rows     BIGINT    NOT NULL DEFAULT 0,
    quarantined_rows BIGINT   NOT NULL DEFAULT 0,
    gold_rows       BIGINT    NOT NULL DEFAULT 0,
    message         VARCHAR
);

CREATE INDEX IF NOT EXISTS ix_quarantine_rule ON ctl.dq_quarantine(rule_id);
CREATE INDEX IF NOT EXISTS ix_quarantine_batch ON ctl.dq_quarantine(batch_id);
