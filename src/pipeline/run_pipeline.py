from __future__ import annotations

import argparse
import json
import logging

from src import db
from src.config import settings, utcnow
from src.ingestion import bronze
from src.transform import gold, silver

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("pipeline")


def new_batch_id() -> str:
    return utcnow().strftime("BATCH-%Y%m%dT%H%M%S%f")


def run(raw_dir: str | None = None, warehouse: str | None = None, full_refresh: bool = False) -> dict:
    batch_id = new_batch_id()
    started = utcnow()
    with db.session(warehouse) as con:
        db.create_schema(con)
        con.execute(
            "INSERT OR REPLACE INTO ctl.pipeline_run (batch_id, run_mode, started_at, status) VALUES (?, ?, ?, ?)",
            [batch_id, "FULL" if full_refresh else "INCREMENTAL", started, "RUNNING"],
        )
        try:
            bronze_stats = bronze.run(con, batch_id, raw_dir)
            silver_stats = silver.run(con, batch_id)
            gold_stats = gold.run(con, batch_id, full_refresh)
            summary = {
                "batch_id": batch_id,
                "bronze": bronze_stats,
                "silver": silver_stats,
                "gold": gold_stats,
            }
            con.execute(
                """
                UPDATE ctl.pipeline_run
                SET finished_at = ?, status = ?, bronze_rows = ?, silver_rows = ?, quarantined_rows = ?, gold_rows = ?, message = ?
                WHERE batch_id = ?
                """,
                [
                    utcnow(),
                    "SUCCESS",
                    sum(bronze_stats.values()),
                    silver_stats["silver_rows"],
                    silver_stats["quarantined_rows"],
                    gold_stats["agg_rows"],
                    "ok",
                    batch_id,
                ],
            )
            return summary
        except Exception as exc:
            con.execute(
                "UPDATE ctl.pipeline_run SET finished_at = ?, status = ?, message = ? WHERE batch_id = ?",
                [utcnow(), "FAILED", str(exc)[:500], batch_id],
            )
            log.exception("pipeline failed")
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Merchant settlement intelligence pipeline")
    parser.add_argument("--raw-dir", default=None)
    parser.add_argument("--warehouse", default=None)
    parser.add_argument("--full-refresh", action="store_true")
    args = parser.parse_args()
    summary = run(args.raw_dir, args.warehouse, args.full_refresh)
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
