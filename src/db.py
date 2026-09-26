from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb

from src.config import settings

_SQL_FILES = ["01_bronze.sql", "02_silver.sql", "03_gold.sql", "04_control.sql"]


def connect(path: str | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    target = path or settings.warehouse_path
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(target, read_only=read_only)


@contextmanager
def session(path: str | None = None, read_only: bool = False):
    con = connect(path, read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def split_statements(sql_text: str) -> list[str]:
    cleaned = re.sub(r"--[^\n]*", "", sql_text)
    return [s.strip() for s in cleaned.split(";") if s.strip()]


def run_sql_file(con: duckdb.DuckDBPyConnection, file_path: str | Path) -> None:
    for stmt in split_statements(Path(file_path).read_text(encoding="utf-8")):
        con.execute(stmt)


def create_schema(con: duckdb.DuckDBPyConnection, sql_dir: str | None = None) -> None:
    base = Path(sql_dir or settings.sql_dir)
    for name in _SQL_FILES:
        run_sql_file(con, base / name)


def query(con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    return con.execute(sql, params or []).fetchall()


def query_dicts(con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


def scalar(con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any] | None = None) -> Any:
    row = con.execute(sql, params or []).fetchone()
    return None if row is None else row[0]


def execute_many(con: duckdb.DuckDBPyConnection, sql: str, rows: Iterable[Sequence[Any]]) -> None:
    payload = list(rows)
    if payload:
        con.executemany(sql, payload)
