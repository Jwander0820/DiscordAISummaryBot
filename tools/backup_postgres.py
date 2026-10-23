#!/usr/bin/env python3
"""Back up PostgreSQL to SQLite and/or a native archive using existing tools."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from copy import copy
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
from sync_postgres_summaries_to_sqlite import export_all_tables, PROJECT_ROOT
from tools.postgres_ssh import add_ssh_arguments, database_connection


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


# Runs inside the database container as the postgres OS user. No credentials
# leave the container; stdin contains only this fixed script and optional names.
CONTAINER_SCRIPT = r'''set -eu
umask 077
archive=$(mktemp /tmp/pg-backup.XXXXXXXX.dump)
trap 'rm -f -- "$archive"' 0
trap 'exit 1' 1 2 15
db_user=${2:-${POSTGRES_USER:-postgres}}
db_name=${1:-${POSTGRES_DB:-$db_user}}
if [ -n "${POSTGRES_PASSWORD:-}" ]; then
    export PGPASSWORD="$POSTGRES_PASSWORD"
fi
export PGCONNECT_TIMEOUT=10
pg_dump --format=custom --no-password --username="$db_user" --dbname="$db_name" --file="$archive"
pg_restore --list "$archive" >/dev/null
pg_restore --file=/dev/null "$archive"
digest=$(sha256sum "$archive")
digest=${digest%% *}
cat "$archive"
printf 'PG_BACKUP_SHA256=%s\n' "$digest" >&2
'''


def build_remote_script(args) -> bytes:
    docker = 'sudo -n docker' if args.sudo else 'docker'
    # Values are shell data, never executable fragments.
    prefix = f'''set -eu
container={shlex.quote(args.container or '')}
if [ -z "$container" ]; then
    candidates=$({docker} ps --filter label=com.docker.compose.service=db --format '{{{{.ID}}}}')
    set -- $candidates
    if [ "$#" -ne 1 ]; then
        printf 'PG_BACKUP_ERROR=container_selection\n' >&2
        exit 1
    fi
    container=$1
fi
{docker} exec -i --user postgres "$container" sh -s -- {shlex.quote(args.database or '')} {shlex.quote(args.db_user or '')} <<'PG_BACKUP_SCRIPT'
'''
    return (prefix + CONTAINER_SCRIPT + 'PG_BACKUP_SCRIPT\n').encode('utf-8')


def build_ssh_command(args):
    ssh = shutil.which('ssh')
    if not ssh:
        raise ValueError('OpenSSH client not found.')
    if not args.ssh_host or args.ssh_host.startswith('-') or any(c.isspace() for c in args.ssh_host):
        raise ValueError('Specify a valid --ssh-host or BACKUP_SSH_HOST.')
    if not 0 <= args.ssh_port <= 65535:
        raise ValueError('Invalid SSH port.')
    if args.timeout <= 0:
        raise ValueError('Timeout must be positive.')
    if args.container and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.container):
        raise ValueError('Invalid container name or ID.')
    command = [ssh, '-T', '-a', '-x', '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
               '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
               '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
               '-o', 'ForkAfterAuthentication=no', '-o', 'RequestTTY=no',
               '-o', 'ClearAllForwardings=yes']
    if args.ssh_key:
        key = Path(args.ssh_key).expanduser()
        if not key.is_file():
            raise ValueError('SSH key file not found (contents are never read by Python).')
        command += ['-o', 'IdentitiesOnly=yes', '-i', str(key)]
    if args.ssh_user:
        command += ['-l', args.ssh_user]
    if args.ssh_port:
        command += ['-p', str(args.ssh_port)]
    # One fixed remote command; all optional remote values travel in stdin.
    return command + [args.ssh_host, 'sh -s']


def download_backup(args):
    command = build_ssh_command(args)
    script = build_remote_script(args)
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError('Output already exists; choose a new --output path.')
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix='.postgres-remote-', suffix='.dump.partial', dir=output.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, 'wb') as stream:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stream,
                                       stderr=subprocess.PIPE,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                _, diagnostics = process.communicate(input=script, timeout=args.timeout)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        if process.returncode:
            if b'PG_BACKUP_ERROR=container_selection' in diagnostics:
                raise ValueError('Cannot select a unique running DB container; specify --container.')
            lowered = diagnostics.lower()
            if b'permission denied' in lowered and (b'docker' in lowered or b'docker.sock' in lowered):
                raise ValueError('Remote Docker access denied; use --sudo if sudo -n docker is authorized.')
            if b'host key verification failed' in lowered:
                raise ValueError('SSH host key verification failed; verify the server fingerprint and known_hosts manually.')
            if b'permission denied (publickey' in lowered:
                raise ValueError('SSH key authentication failed; check SSH user, key path or local ssh-agent.')
            raise ValueError('SSH or remote backup failed; check SSH access, Docker permissions and DB authentication. Private diagnostics suppressed.')
        match = re.fullmatch(rb'PG_BACKUP_SHA256=([0-9a-f]{64})\r?\n', diagnostics)
        if match is None:
            raise ValueError('Remote verification reported warnings or an invalid response; archive not published. Private diagnostics suppressed.')
        with temporary.open('rb') as stream:
            if stream.read(5) != b'PGDMP':
                raise ValueError('Invalid PostgreSQL archive header; archive not published.')
            stream.seek(0)
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != match[1].decode('ascii'):
            raise ValueError('Archive checksum mismatch; archive not published.')
        os.link(temporary, output)
        return output, digest
    finally:
        temporary.unlink(missing_ok=True)


def _now():
    return datetime.now(timezone(timedelta(hours=8)))


def parse_args(argv=None):
    load_local_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--format', choices=('both', 'sqlite', 'postgres'), default=None,
                        help='Default: both. Legacy --output FILE defaults to postgres.')
    parser.add_argument('--output', type=Path, help='Single-format output file; incompatible with both/--output-dir.')
    parser.add_argument('--output-dir', type=Path, help='New batch directory; default: project backups/<Taipei timestamp>.')
    parser.add_argument('--pg-bin', help='Directory containing pg_dump and pg_restore; client must support source server version.')
    parser.add_argument('--native-source', choices=('auto', 'local', 'remote'), default='auto',
                        help='auto uses remote Docker when SSH is enabled, otherwise local pg_dump.')
    add_ssh_arguments(parser)
    parser.add_argument('--container', help='Remote DB container; auto-select unique Compose service=db container.')
    parser.add_argument('--database', help='Override DB name for both formats; otherwise use DATABASE_URL DB name.')
    parser.add_argument('--db-user', help='Remote native DB role; defaults to container POSTGRES_USER.')
    parser.add_argument('--sudo', action='store_true', help='Use sudo -n docker for remote native backup.')
    parser.add_argument('--timeout', type=int, default=600, help='Remote native backup timeout in seconds.')
    args = parser.parse_args(argv)
    args.format = args.format or ('postgres' if args.output else 'both')
    if args.output and (args.format == 'both' or args.output_dir):
        parser.error('--output is for a single format; use --output-dir for both.')
    if args.native_source == 'remote' and not args.ssh_host:
        parser.error('--native-source remote requires --ssh-host or BACKUP_SSH_HOST.')
    return args


def _database_url(args):
    url = os.getenv('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL is not configured for this backup format.')
    if args.database:
        from psycopg2.extensions import make_dsn
        url = make_dsn(url, dbname=args.database)
    return url


def _run_format(kind, output, args):
    if kind == 'sqlite':
        with database_connection(_database_url(args), args) as url:
            counts = export_all_tables(url, output)
        details = {'tables': counts, 'validation': 'sqlite_integrity_check'}
    elif args.native_source == 'remote' or (args.native_source == 'auto' and args.ssh_host):
        remote = copy(args)
        remote.output = output
        # For both, use the same explicit DB name as SQLite, not the container default.
        if args.format == 'both' and not remote.database:
            from psycopg2.extensions import parse_dsn
            remote.database = parse_dsn(_database_url(args)).get('dbname')
            if not remote.database:
                raise RuntimeError('Specify --database for both-format remote backups.')
        download_backup(remote)
        details = {'validation': 'remote_archive_decode_and_transfer_sha256', 'restore_tested': False}
    else:
        dump = find_client('pg_dump', args.pg_bin)
        restore = find_client('pg_restore', args.pg_bin)
        with database_connection(_database_url(args), args) as url:
            backup_database(output, url, dump, restore)
        details = {'validation': 'local_archive_decode', 'restore_tested': False}
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'status': 'success', 'file': output.name, 'bytes': output.stat().st_size,
            'sha256': digest, **details}


def run_backup(args):
    kinds = ('postgres', 'sqlite') if args.format == 'both' else (args.format,)
    if args.output:
        # Preserve the old single-file interface without introducing a sidecar into
        # an existing directory or changing the caller's requested file layout.
        result = _run_format(kinds[0], args.output.resolve(), args)
        print(f'Backup: {args.output.resolve()}')
        print(f'Bytes: {result["bytes"]}; SHA256: {result["sha256"]}')
        return 0
    directory = (args.output_dir or PROJECT_ROOT / 'backups' / _now().strftime('%Y%m%d_%H%M%S_%f')).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {'version': 1, 'started_at': _now().isoformat(),
                'snapshot_consistency': 'independent', 'backups': {}}
    for kind in kinds:
        output = directory / ('database.dump' if kind == 'postgres' else 'database.sqlite3')
        started = _now().isoformat()
        try:
            result = _run_format(kind, output, args)
        except Exception as exc:
            # Never serialize exception text: it can contain credentials or row data.
            result = {'status': 'failed', 'error_type': type(exc).__name__,
                      'error': 'backup_failed_private_details_suppressed'}
        result['started_at'] = started
        result['finished_at'] = _now().isoformat()
        manifest['backups'][kind] = result
        print(f'{kind}: {result["status"]}')
    success = sum(item['status'] == 'success' for item in manifest['backups'].values())
    manifest['status'] = 'success' if success == len(kinds) else ('partial_success' if success else 'failed')
    manifest['finished_at'] = _now().isoformat()
    temporary = directory / '.manifest.partial'
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.link(temporary, directory / 'manifest.json')
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Backup directory: {directory}')
    print(f'Overall: {manifest["status"]}; manifest.json contains checksums and per-format results.')
    print('Formats use independent snapshots; native database restore has not been tested.')
    return 0 if manifest['status'] == 'success' else 1


def main(argv=None) -> int:
    try:
        return run_backup(parse_args(argv))
    except Exception as exc:
        print(f'Backup failed ({type(exc).__name__}); private details suppressed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
