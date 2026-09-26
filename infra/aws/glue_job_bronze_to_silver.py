from __future__ import annotations

import sys

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import Window
from pyspark.sql import functions as F

ARGS = getResolvedOptions(
    sys.argv,
    ["JOB_NAME", "landing_bucket", "lakehouse_bucket", "watermark", "late_threshold_sec", "sla_threshold_sec"],
)

sc = SparkContext()
glue = GlueContext(sc)
spark = glue.spark_session
job = Job(glue)
job.init(ARGS["JOB_NAME"], ARGS)

LANDING = f"s3://{ARGS['landing_bucket']}"
LAKEHOUSE = f"s3://{ARGS['lakehouse_bucket']}"
WATERMARK = ARGS["watermark"]
LATE_SEC = int(ARGS["late_threshold_sec"])
SLA_SEC = int(ARGS["sla_threshold_sec"])


def read_bronze(entity: str):
    return (
        spark.read.format("delta")
        .load(f"{LANDING}/bronze/{entity}")
        .where(F.col("_ingested_at") > F.lit(WATERMARK).cast("timestamp"))
    )


def write_silver(df, entity: str, partition: str | None = None):
    writer = df.write.format("delta").mode("append").option("mergeSchema", "false")
    if partition:
        writer = writer.partitionBy(partition)
    writer.save(f"{LAKEHOUSE}/silver/{entity}")


def write_quarantine(df, entity: str):
    (
        df.withColumn("source_table", F.lit(entity))
        .withColumn("detected_at", F.current_timestamp())
        .write.format("delta")
        .mode("append")
        .partitionBy("source_table")
        .save(f"{LAKEHOUSE}/ctl/dq_quarantine")
    )


merchant = (
    spark.read.format("delta")
    .load(f"{LANDING}/bronze/merchant")
    .select(
        F.trim("merchant_id").alias("merchant_id"),
        F.col("merchant_name"),
        F.upper(F.trim("risk_level")).alias("risk_level"),
        F.to_date("effective_from").alias("effective_from"),
        F.coalesce(F.to_date("effective_to"), F.lit("9999-12-31").cast("date")).alias("effective_to"),
    )
    .dropDuplicates(["merchant_id", "effective_from"])
)

txn_raw = read_bronze("transactions")
txn_typed = (
    txn_raw.select(
        F.trim("transaction_id").alias("transaction_id"),
        F.nullif(F.trim("merchant_id"), F.lit("")).alias("merchant_id"),
        F.nullif(F.trim("customer_id"), F.lit("")).alias("customer_id"),
        F.to_timestamp("transaction_ts").alias("transaction_ts"),
        F.col("amount").cast("decimal(18,2)").alias("amount"),
        F.upper(F.trim("currency")).alias("currency"),
        F.upper(F.trim("status")).alias("status"),
        F.upper(F.trim("payment_channel")).alias("payment_channel"),
        F.col("_batch_id"),
        F.col("_ingested_at"),
    )
    .withColumn(
        "dup_rank",
        F.row_number().over(Window.partitionBy("transaction_id").orderBy(F.col("_ingested_at").desc())),
    )
    .withColumn("transaction_date", F.to_date("transaction_ts"))
)

txn_flagged = txn_typed.withColumn(
    "reject_rule",
    F.when(F.col("transaction_id").isNull() | (F.length("transaction_id") == 0), F.lit("TXN-001"))
    .when(F.col("transaction_ts").isNull(), F.lit("TXN-002"))
    .when(F.col("amount").isNull(), F.lit("TXN-003"))
    .when(F.col("amount") < 0, F.lit("TXN-004"))
    .when(F.col("merchant_id").isNull(), F.lit("TXN-005"))
    .when(~F.col("currency").isin("INR"), F.lit("TXN-006"))
    .when(~F.col("status").isin("SUCCESS", "FAILED", "REVERSED"), F.lit("TXN-007"))
    .when(~F.col("payment_channel").isin("POS", "ONLINE", "QR"), F.lit("TXN-008")),
)

write_quarantine(txn_flagged.where(F.col("reject_rule").isNotNull()), "transactions")

txn_clean = (
    txn_flagged.where(F.col("reject_rule").isNull() & (F.col("dup_rank") == 1))
    .join(merchant.select("merchant_id").distinct().withColumn("known", F.lit(True)), "merchant_id", "left")
    .withColumn("is_merchant_known", F.coalesce(F.col("known"), F.lit(False)))
    .drop("known", "reject_rule", "dup_rank")
)
write_silver(txn_clean, "transactions", "transaction_date")

stl_raw = read_bronze("settlements")
stl_typed = (
    stl_raw.select(
        F.trim("settlement_id").alias("settlement_id"),
        F.trim("transaction_id").alias("transaction_id"),
        F.to_timestamp("settlement_ts").alias("settlement_ts"),
        F.col("settlement_amount").cast("decimal(18,2)").alias("settlement_amount"),
        F.upper(F.trim("settlement_status")).alias("settlement_status"),
        F.col("settlement_batch"),
        F.col("_batch_id"),
        F.col("_ingested_at"),
    )
    .withColumn("settlement_date", F.to_date("settlement_ts"))
    .withColumn(
        "dup_rank",
        F.row_number().over(Window.partitionBy("settlement_id").orderBy(F.col("_ingested_at").desc())),
    )
    .withColumn(
        "reject_rule",
        F.when(F.col("settlement_id").isNull(), F.lit("STL-001"))
        .when(F.col("settlement_ts").isNull(), F.lit("STL-002"))
        .when(F.col("settlement_amount").isNull(), F.lit("STL-003"))
        .when(F.col("settlement_amount") < 0, F.lit("STL-004"))
        .when(~F.col("settlement_status").isin("SETTLED", "PENDING", "FAILED"), F.lit("STL-005")),
    )
)

write_quarantine(stl_typed.where(F.col("reject_rule").isNotNull()), "settlements")

all_txn_ids = spark.read.format("delta").load(f"{LAKEHOUSE}/silver/transactions").select("transaction_id").distinct()
stl_clean = (
    stl_typed.where(F.col("reject_rule").isNull() & (F.col("dup_rank") == 1))
    .join(all_txn_ids.withColumn("matched", F.lit(True)), "transaction_id", "left")
    .withColumn("is_orphan", F.coalesce(~F.col("matched"), F.lit(True)))
    .drop("matched", "reject_rule", "dup_rank")
)
write_silver(stl_clean, "settlements", "settlement_date")

evt_raw = read_bronze("payment_events")
evt_clean = (
    evt_raw.select(
        F.trim("event_id").alias("event_id"),
        F.trim("transaction_id").alias("transaction_id"),
        F.upper(F.trim("event_type")).alias("event_type"),
        F.to_timestamp("event_ts").alias("event_ts"),
        F.to_timestamp("ingestion_ts").alias("ingestion_ts"),
        F.col("processing_ms").cast("int").alias("processing_ms"),
        F.col("_batch_id"),
        F.col("_ingested_at"),
    )
    .withColumn("event_date", F.to_date("event_ts"))
    .withColumn("ingestion_lag_sec", F.unix_timestamp("ingestion_ts") - F.unix_timestamp("event_ts"))
    .withColumn("is_late_arriving", F.col("ingestion_lag_sec") > LATE_SEC)
    .withColumn("is_sla_breach", F.col("ingestion_lag_sec") > SLA_SEC)
    .withColumn(
        "dup_rank",
        F.row_number().over(Window.partitionBy("event_id").orderBy(F.col("ingestion_ts").asc())),
    )
    .where(F.col("dup_rank") == 1)
    .drop("dup_rank")
)
write_silver(evt_clean, "payment_events", "event_date")

job.commit()
