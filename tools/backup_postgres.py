#!/usr/bin/env python3
"""Export one complete PostgreSQL database without exposing connection secrets."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_postgres_summaries_to_sqlite import load_local_env


def find_client(name: str, directory: str | None) -> str:
    if directory:
        candidate = Path(directory) / (name + ('.exe' if os.name == 'nt' else ''))
        if candidate.is_file():
            return str(candidate)
    else:
        candidate = shutil.which(name)
        if candidate:
            return candidate
    raise RuntimeError('PostgreSQL client missing; install matching pg_dump/pg_restore or use --pg-bin.')


def run_client(command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode:
        detail = result.stderr.decode('utf-8', errors='replace').lower()
        category = 'client error'
        for marker in ('version mismatch', 'permission denied', 'timeout',
                       'connection refused', 'could not translate host name',
                       'password authentication failed'):
            if marker in detail:
                category = marker
                break
        raise RuntimeError(f'{Path(command[0]).stem} failed ({category}); private diagnostics suppressed.')
    if result.stderr:
        raise RuntimeError(f'{Path(command[0]).stem} produced diagnostics; archive not published. Private diagnostics suppressed.')


def backup_database(output: Path, url: str, pg_dump: str, pg_restore: str) -> str:
    output = output.resolve()
    if output.exists():
        raise RuntimeError('Output already exists; refusing to overwrite.')
    output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env['PGDATABASE'] = url
    env['PGCONNECT_TIMEOUT'] = '10'
    fd, temporary = tempfile.mkstemp(prefix='.postgres-', suffix='.dump.partial', dir=output.parent)
    os.close(fd)
    temp = Path(temporary)
    try:
        run_client([pg_dump, '--format=custom', '--no-password', '--file', str(temp)], env)
        if temp.stat().st_size == 0:
            raise RuntimeError('Empty archive; backup not published.')
        run_client([pg_restore, '--list', str(temp)], env)
        run_client([pg_restore, '--file', os.devnull, str(temp)], env)
        with temp.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        # Hard-link publication is atomic and fails if the destination already exists.
        os.link(temp, output)
        return digest
    finally:
        temp.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S_%f')
    parser.add_argument('--output', type=Path, default=Path('backups') / f'postgres_{stamp}.dump')
    parser.add_argument('--pg-bin', help='Directory containing pg_dump and pg_restore; client must support source server version.')
    args = parser.parse_args(argv)
    try:
        dump = find_client('pg_dump', args.pg_bin)
        restore = find_client('pg_restore', args.pg_bin)
        load_local_env()
        url = os.getenv('DATABASE_URL')
        if not url:
            raise RuntimeError('DATABASE_URL is not configured.')
        digest = backup_database(args.output, url, dump, restore)
    except (OSError, RuntimeError) as exc:
        # OSError may contain local paths; never include child process environment.
        print(str(exc) if isinstance(exc, RuntimeError) else 'Local file/process operation failed.', file=sys.stderr)
        return 1
    print(f'Backup: {args.output.resolve()}')
    print(f'Bytes: {args.output.stat().st_size}; SHA256: {digest}')
    print('Archive listing and full decoding passed; database restore has not been tested.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
