from __future__ import annotations

import hashlib
from pathlib import Path

import duckdb

from src.config import settings, utcnow

ENTITIES: dict[str, dict] = {
    "transactions": {
        "pattern": "transactions*.csv",
        "columns": ["transaction_id", "merchant_id", "customer_id", "transaction_ts", "amount", "currency", "status", "payment_channel"],
    },
    "settlements": {
        "pattern": "settlements*.csv",
        "columns": ["settlement_id", "transaction_id", "settlement_ts", "settlement_amount", "settlement_status", "settlement_batch"],
    },
    "merchant": {
        "pattern": "merchant*.csv",
        "columns": ["merchant_id", "merchant_name", "merchant_category", "country", "risk_level", "effective_from", "effective_to"],
    },
    "payment_events": {
        "pattern": "payment_events*.csv",
        "columns": ["event_id", "transaction_id", "event_type", "event_ts", "ingestion_ts", "processing_ms"],
    },
}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover(raw_dir: str | None = None) -> list[tuple[str, Path]]:
    base = Path(raw_dir or settings.raw_dir)
    found: list[tuple[str, Path]] = []
    for entity, meta in ENTITIES.items():
        for path in sorted(base.glob(meta["pattern"])):
            found.append((entity, path))
    return found


def already_loaded(con: duckdb.DuckDBPyConnection, entity: str, path: Path, digest: str) -> bool:
    row = con.execute(
        "SELECT file_hash FROM ctl.load_file_audit WHERE source_file = ? AND target_table = ?",
        [path.name, f"bronze.{entity}"],
    ).fetchone()
    return bool(row) and row[0] == digest


def load_file(con: duckdb.DuckDBPyConnection, entity: str, path: Path, batch_id: str) -> int:
    digest = file_hash(path)
    if already_loaded(con, entity, path, digest):
        return 0

    cols = ENTITIES[entity]["columns"]
    select_cols = ", ".join(f'"{c}"' for c in cols)
    hash_expr = "md5(concat_ws('|', " + ", ".join(f'coalesce("{c}", chr(0))' for c in cols) + "))"
    now = utcnow()

    con.execute(
        f"""
        INSERT INTO bronze.{entity}
        SELECT {select_cols},
               ? AS _source_file,
               row_number() OVER () AS _source_row_num,
               {hash_expr} AS _row_hash,
               ? AS _batch_id,
               ? AS _ingested_at
        FROM read_csv(?, header = true, all_varchar = true, ignore_errors = true)
        """,
        [path.name, batch_id, now, str(path)],
    )
    inserted = con.execute(
        "SELECT count(*) FROM bronze." + entity + " WHERE _batch_id = ? AND _source_file = ?",
        [batch_id, path.name],
    ).fetchone()[0]

    con.execute(
        """
        INSERT OR REPLACE INTO ctl.load_file_audit
            (source_file, batch_id, target_table, file_hash, row_count, loaded_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [path.name, batch_id, f"bronze.{entity}", digest, inserted, now],
    )
    return inserted


def run(con: duckdb.DuckDBPyConnection, batch_id: str, raw_dir: str | None = None) -> dict[str, int]:
    stats: dict[str, int] = {}
    for entity, path in discover(raw_dir):
        loaded = load_file(con, entity, path, batch_id)
        stats[path.name] = loaded
    return stats
