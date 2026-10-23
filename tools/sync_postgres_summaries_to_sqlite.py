#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from pathlib import Path
from typing import Iterable, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from tools.postgres_ssh import add_ssh_arguments, database_connection

from discord_bot.db.schema import (
    CREATE_DEEPFAKER_EVENT_INDEXES_SQL,
    DEEPFAKER_EVENT_COLUMNS,
    SQLITE_CREATE_DEEPFAKER_EVENTS_SQL,
    SQLITE_CREATE_SUMMARIES_SQL,
    SUMMARY_COLUMNS,
)

DEEPFAKER_SYNC_COLUMNS = ("id", *DEEPFAKER_EVENT_COLUMNS)
SYNC_TABLES = {
    "summaries": {
        "columns": SUMMARY_COLUMNS,
        "create_sql": SQLITE_CREATE_SUMMARIES_SQL,
        "indexes": (),
    },
    "deepfaker_events": {
        "columns": DEEPFAKER_SYNC_COLUMNS,
        "create_sql": SQLITE_CREATE_DEEPFAKER_EVENTS_SQL,
        "indexes": CREATE_DEEPFAKER_EVENT_INDEXES_SQL,
    },
}


def _load_simple_env(path: Path) -> None:
    """Load a small subset of `.env` syntax without introducing a runtime dependency."""
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
    """Load project-level env files in the same order as local bot development."""
    root = PROJECT_ROOT
    _load_simple_env(root / ".env")
    _load_simple_env(root / "discord_bot" / ".env")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments for the backup tool."""
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S_%f')
    default_output = os.getenv("LOCAL_BACKUP_SQLITE_PATH", f"postgres_backup_{stamp}.db")
    parser = argparse.ArgumentParser(
        description="Export all PostgreSQL user tables to a new SQLite snapshot."
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL connection URL. Prefer using DATABASE_URL in .env instead of passing secrets on the command line.",
    )
    parser.add_argument(
        "--output",
        default=default_output,
        help="New SQLite output path. Defaults to LOCAL_BACKUP_SQLITE_PATH or a timestamped file.",
    )
    parser.add_argument(
        "--table",
        choices=("all", "history", *SYNC_TABLES),
        default="all",
        help="all exports every user table; history selects only the two legacy history tables.",
    )
    parser.add_argument(
        "--mode",
        choices=("incremental", "replace", "upsert"),
        default=None,
        help=(
            "all always creates a full new snapshot (incremental is rejected). "
            "For history/single legacy table: incremental is the default; "
            "replace recreates tables; upsert updates rows by id."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of rows to copy per selected table, useful for smoke tests.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Connect and count fetched rows without writing SQLite.",
    )
    add_ssh_arguments(parser)
    args = parser.parse_args(argv)
    supplied = list(sys.argv[1:] if argv is None else argv)
    if args.table == 'all' and Path(args.output).exists() and not any(
        item == '--output' or item.startswith('--output=') for item in supplied
    ):
        configured = Path(args.output)
        args.output = str(configured.with_name(f'{configured.stem}_{stamp}{configured.suffix}'))
    return args


def _coerce_sqlite_value(value):
    """Convert database-native values into SQLite-friendly scalar values."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, (Decimal, UUID, timedelta)):
        return str(value)
    if isinstance(value, int) and not -(2**63) <= value < 2**63:
        return str(value)
    if value is not None and not isinstance(value, (str, int, float, bytes)):
        return str(value)
    return value


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class ExportValidationError(ValueError):
    """A fixed diagnostic that is safe to display without source data."""


def export_all_tables(database_url: str, output: Path, *, dry_run=False, limit=None):
    """Stream one read-only repeatable-read snapshot into a new SQLite file."""
    import psycopg2
    from psycopg2 import sql

    if limit is not None and limit <= 0:
        raise ExportValidationError('--limit must be greater than 0')
    output = output.resolve()
    if not dry_run and output.exists():
        raise ExportValidationError('Output exists; choose a new --output path to preserve the previous backup.')
    temporary = None
    target = None
    counts = {}
    try:
        if not dry_run:
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, filename = tempfile.mkstemp(dir=output.parent, suffix='.db', prefix='.postgres-export-')
            os.close(fd)
            temporary = Path(filename)
            target = sqlite3.connect(temporary)
            target.execute('BEGIN')
        source = psycopg2.connect(database_url, connect_timeout=10)
        try:
            source.set_session(isolation_level='REPEATABLE READ', readonly=True, autocommit=False)
            with source.cursor() as metadata:
                # Fail for insufficient privileges/RLS rather than silently exporting a subset.
                metadata.execute('SET LOCAL row_security = off')
                metadata.execute("""
                    SELECT n.nspname, c.relname FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind IN ('r', 'p', 'f') AND NOT c.relispartition
                      AND n.nspname <> 'information_schema'
                      AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                    ORDER BY n.nspname, c.relname
                """)
                tables = metadata.fetchall()
            names = set()
            for index, (schema, table) in enumerate(tables):
                name = table if schema == 'public' else f'{schema}.{table}'
                if name.casefold() in names or name.casefold().startswith('sqlite_'):
                    raise ExportValidationError('SQLite table naming collision; export requires PostgreSQL native backup.')
                names.add(name.casefold())
                query = sql.SQL('SELECT * FROM {}.{}').format(sql.Identifier(schema), sql.Identifier(table))
                if limit is not None:
                    query += sql.SQL(' LIMIT {}').format(sql.Literal(limit))
                with source.cursor(name=f'sqlite_export_{index}') as cursor:
                    cursor.execute(query)
                    batch = cursor.fetchmany(1000)
                    # Named cursor description is available after the initial fetch, even for empty tables.
                    columns = [column[0] for column in cursor.description]
                    if len({column.casefold() for column in columns}) != len(columns):
                        raise ExportValidationError('SQLite column naming collision; use a PostgreSQL native backup.')
                    if not columns:
                        raise ExportValidationError('SQLite cannot represent a zero-column table.')
                    if target is not None:
                        # No type affinity: preserve Python scalars without SQLite numeric coercion.
                        target.execute(f'CREATE TABLE {_quote_identifier(name)} (' +
                                       ', '.join(_quote_identifier(column) for column in columns) + ')')
                    count = 0
                    while batch:
                        if target is not None:
                            target.executemany(f'INSERT INTO {_quote_identifier(name)} VALUES (' +
                                               ', '.join('?' for _ in columns) + ')',
                                               (_coerce_row(row) for row in batch))
                        count += len(batch)
                        batch = cursor.fetchmany(1000)
                    counts[name] = count
            source.rollback()
        finally:
            source.close()
        if target is not None:
            target.commit()
            if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ExportValidationError('SQLite integrity check failed.')
            target.close()
            target = None
            os.link(temporary, output)  # Atomic publication; never overwrite a racing writer.
        return counts
    finally:
        if target is not None:
            target.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _coerce_row(row: Sequence[object]) -> tuple[object, ...]:
    """Normalize every field in a fetched PostgreSQL row before writing to SQLite."""
    return tuple(_coerce_sqlite_value(value) for value in row)


def fetch_table(
    database_url: str,
    table: str,
    *,
    min_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[tuple[object, ...]]:
    """Fetch a supported PostgreSQL table in schema order, optionally incrementally."""
    if table not in SYNC_TABLES:
        raise ValueError(f"Unsupported sync table: {table}")
    try:
        import psycopg2
    except ModuleNotFoundError as exc:
        raise RuntimeError("psycopg2 is not installed. Install requirements.txt before syncing PostgreSQL.") from exc

    columns = ", ".join(SYNC_TABLES[table]["columns"])
    sql = f"SELECT {columns} FROM {table} ORDER BY id"
    params: list[object] = []
    if min_id is not None:
        sql = f"SELECT {columns} FROM {table} WHERE id > %s ORDER BY id"
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


def fetch_summaries(
    database_url: str,
    *,
    min_id: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[tuple[object, ...]]:
    """Backward-compatible summaries fetch helper."""
    return fetch_table(database_url, "summaries", min_id=min_id, limit=limit)


def get_local_max_id(sqlite_path: Path, table: str) -> Optional[int]:
    """Read a supported local backup table's latest id for incremental sync mode."""
    if table not in SYNC_TABLES:
        raise ValueError(f"Unsupported sync table: {table}")
    if not sqlite_path.exists():
        return None

    conn = None
    try:
        conn = sqlite3.connect(sqlite_path)
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            return None
        row = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        if conn is not None:
            conn.close()

    if row is None or row[0] is None:
        return None
    return int(row[0])


def get_local_max_summary_id(sqlite_path: Path) -> Optional[int]:
    """Backward-compatible summaries incremental cursor helper."""
    return get_local_max_id(sqlite_path, "summaries")


def _write_table(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[Sequence[object]],
    *,
    mode: str,
) -> int:
    if table not in SYNC_TABLES:
        raise ValueError(f"Unsupported sync table: {table}")

    definition = SYNC_TABLES[table]
    columns = definition["columns"]
    rows = [tuple(row) for row in rows]
    if mode == "replace":
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(definition["create_sql"])
    for index_sql in definition["indexes"]:
        conn.execute(index_sql)
    placeholders = ", ".join(["?"] * len(columns))
    column_sql = ", ".join(columns)
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} ({column_sql}) VALUES ({placeholders})",
        rows,
    )
    return len(rows)


def write_tables_to_sqlite(
    rows_by_table: dict[str, Iterable[Sequence[object]]],
    sqlite_path: Path,
    *,
    mode: str,
) -> dict[str, int]:
    """Write selected tables into one SQLite transaction."""
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sqlite_path)
    try:
        written = {
            table: _write_table(conn, table, rows, mode=mode)
            for table, rows in rows_by_table.items()
        }
        conn.commit()
        return written
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def write_summaries_to_sqlite(rows: Iterable[Sequence[object]], sqlite_path: Path, *, mode: str) -> int:
    """Backward-compatible summaries SQLite writer."""
    return write_tables_to_sqlite({"summaries": rows}, sqlite_path, mode=mode)["summaries"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Export all user tables, or explicitly sync legacy history tables."""
    load_local_env()
    args = parse_args(argv)

    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set. Add it to .env / discord_bot/.env or pass --database-url.", file=sys.stderr)
        return 2

    try:
        with database_connection(database_url, args) as connected_url:
            return _run_export(args, connected_url)
    except Exception as exc:
        print(f'Backup connection failed ({type(exc).__name__}); private details suppressed.', file=sys.stderr)
        if isinstance(exc, ValueError) and str(exc).startswith(('SSH ', 'Invalid SSH', 'OpenSSH ', 'SSH backup')):
            print(str(exc), file=sys.stderr)
        return 1


def _run_export(args, database_url):
    output = Path(args.output)
    if args.table == 'all':
        if args.mode == 'incremental':
            print('All-table export is a full snapshot. Use --table history for incremental sync.', file=sys.stderr)
            return 2
        try:
            counts = export_all_tables(database_url, output, dry_run=args.dry_run, limit=args.limit)
        except Exception as exc:
            print(f'All-table export failed ({type(exc).__name__}); connection/data details suppressed.', file=sys.stderr)
            if isinstance(exc, ExportValidationError):
                print(str(exc), file=sys.stderr)
            return 1
        for table, count in counts.items():
            print(f'{table}: {count} row(s)')
        print('Dry run: no SQLite file written.' if args.dry_run else f'SQLite snapshot: {output.resolve()} (integrity_check: ok)')
        if args.limit:
            print('WARNING: --limit was used; this is a partial export.')
        print('Data export only: PostgreSQL indexes, constraints, roles, views and functions are not preserved.')
        return 0
    args.mode = args.mode or 'incremental'
    selected_tables = list(SYNC_TABLES) if args.table == "history" else [args.table]
    rows_by_table: dict[str, list[tuple[object, ...]]] = {}
    for table in selected_tables:
        min_id = None
        if args.mode == "incremental":
            max_id = get_local_max_id(output, table)
            if max_id is not None:
                min_id = max_id
                print(f"Incremental mode: local max {table}.id is {max_id}; fetching rows with id > {max_id}.")
            else:
                print(f"Incremental mode: no local {table} table found; fetching all rows.")

        try:
            rows = fetch_table(database_url, table, min_id=min_id, limit=args.limit)
        except Exception as exc:
            print(f"Failed to fetch PostgreSQL {table} ({type(exc).__name__}); private details suppressed.", file=sys.stderr)
            return 1
        rows_by_table[table] = rows
        print(f"Fetched {len(rows)} row(s) from PostgreSQL {table}.")

    if args.dry_run:
        print("Dry run enabled; SQLite was not written.")
        return 0

    try:
        written = write_tables_to_sqlite(rows_by_table, output, mode=args.mode)
    except Exception as exc:
        print(f"Failed to write SQLite backup ({type(exc).__name__}); private details suppressed.", file=sys.stderr)
        return 1

    for table, count in written.items():
        print(f"Wrote {count} {table} row(s) to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
