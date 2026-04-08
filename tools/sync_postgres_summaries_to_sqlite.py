#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence


SUMMARY_COLUMNS = (
    "id",
    "channel_id",
    "user_id",
    "command",
    "question",
    "prompt",
    "summary",
    "call_time",
)

CREATE_SUMMARIES_SQL = """
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY,
    channel_id TEXT,
    user_id TEXT,
    command TEXT,
    question TEXT,
    prompt TEXT,
    summary TEXT,
    call_time TEXT
);
"""


def _load_simple_env(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def load_local_env() -> None:
    root = Path(__file__).resolve().parents[1]
    _load_simple_env(root / ".env")
    _load_simple_env(root / "discord_bot" / ".env")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    default_output = os.getenv("LOCAL_BACKUP_SQLITE_PATH", "postgres_summaries_backup.db")
    parser = argparse.ArgumentParser(
        description="Copy the PostgreSQL summaries table into a local SQLite database."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL connection URL. Prefer using DATABASE_URL in .env instead of passing secrets on the command line.",
    )
    parser.add_argument(
        "--output",
        default=default_output,
        help="SQLite output path. Defaults to LOCAL_BACKUP_SQLITE_PATH or postgres_summaries_backup.db.",
    )
    parser.add_argument(
        "--mode",
        choices=("incremental", "replace", "upsert"),
        default="incremental",
        help=(
            "incremental fetches rows newer than the local max id; "
            "replace recreates summaries; upsert fetches all rows and INSERT OR REPLACE by id."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of rows to copy, useful for smoke tests.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Connect and count fetched rows without writing SQLite.",
    )
    return parser.parse_args(argv)


def _coerce_sqlite_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _coerce_row(row: Sequence[object]) -> tuple[object, ...]:
    return tuple(_coerce_sqlite_value(value) for value in row)


def fetch_summaries(
    database_url: str,
    *,
    min_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[tuple[object, ...]]:
    try:
        import psycopg2
    except ModuleNotFoundError as exc:
        raise RuntimeError("psycopg2 is not installed. Install requirements.txt before syncing PostgreSQL.") from exc

    columns = ", ".join(SUMMARY_COLUMNS)
    sql = f"SELECT {columns} FROM summaries ORDER BY id"
    params: list[object] = []
    if min_id is not None:
        sql = f"SELECT {columns} FROM summaries WHERE id > %s ORDER BY id"
        params.append(min_id)
    if limit is not None:
        if limit <= 0:
            raise ValueError("--limit must be greater than 0")
        sql += " LIMIT %s"
        params.append(limit)

    with psycopg2.connect(database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [_coerce_row(row) for row in cur.fetchall()]


def get_local_max_summary_id(sqlite_path: Path) -> Optional[int]:
    if not sqlite_path.exists():
        return None

    try:
        with sqlite3.connect(sqlite_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'summaries'"
            ).fetchone()
            if not exists:
                return None
            row = conn.execute("SELECT MAX(id) FROM summaries").fetchone()
    except sqlite3.DatabaseError:
        return None

    if row is None or row[0] is None:
        return None
    return int(row[0])


def write_summaries_to_sqlite(rows: Iterable[Sequence[object]], sqlite_path: Path, *, mode: str) -> int:
    rows = [tuple(row) for row in rows]
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    placeholders = ", ".join(["?"] * len(SUMMARY_COLUMNS))
    columns = ", ".join(SUMMARY_COLUMNS)

    with sqlite3.connect(sqlite_path) as conn:
        if mode == "replace":
            conn.execute("DROP TABLE IF EXISTS summaries")
        conn.execute(CREATE_SUMMARIES_SQL)
        conn.executemany(
            f"INSERT OR REPLACE INTO summaries ({columns}) VALUES ({placeholders})",
            rows,
        )
        conn.commit()

    return len(rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    load_local_env()
    args = parse_args(argv)

    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set. Add it to .env / discord_bot/.env or pass --database-url.", file=sys.stderr)
        return 2

    output = Path(args.output)
    min_id = None
    if args.mode == "incremental":
        max_id = get_local_max_summary_id(output)
        if max_id is not None:
            min_id = max_id
            print(f"Incremental mode: local max summaries.id is {max_id}; fetching rows with id > {max_id}.")
        else:
            print("Incremental mode: no local summaries table found; fetching all rows.")

    try:
        rows = fetch_summaries(database_url, min_id=min_id, limit=args.limit)
    except Exception as exc:
        print(f"Failed to fetch PostgreSQL summaries: {exc}", file=sys.stderr)
        return 1

    print(f"Fetched {len(rows)} row(s) from PostgreSQL summaries.")
    if args.dry_run:
        print("Dry run enabled; SQLite was not written.")
        return 0

    try:
        written = write_summaries_to_sqlite(rows, output, mode=args.mode)
    except Exception as exc:
        print(f"Failed to write SQLite backup: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {written} row(s) to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
