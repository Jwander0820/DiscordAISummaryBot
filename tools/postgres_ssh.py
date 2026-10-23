"""Optional local OpenSSH tunnel; never reads private key contents."""
from contextlib import contextmanager
from pathlib import Path
import os
import re
import shutil
import socket
import subprocess
import time


def add_ssh_arguments(parser):
    parser.add_argument('--ssh-host', default=os.getenv('BACKUP_SSH_HOST'), help='SSH host or existing SSH config alias; enables tunnel.')
    parser.add_argument('--ssh-user', default=os.getenv('BACKUP_SSH_USER'), help='Optional SSH user; otherwise use SSH config.')
    parser.add_argument('--ssh-key', default=os.getenv('BACKUP_SSH_KEY'), help='Local private-key path, passed to OpenSSH without reading it.')
    parser.add_argument('--ssh-port', type=int, default=int(os.getenv('BACKUP_SSH_PORT', '0')), help='SSH port; 0 uses SSH config (normally 22).')
    parser.add_argument('--ssh-db-host', default=os.getenv('BACKUP_SSH_DB_HOST', '127.0.0.1'), help='DB host as reachable from SSH server.')
    parser.add_argument('--ssh-db-port', type=int, default=int(os.getenv('BACKUP_SSH_DB_PORT', '5432')), help='DB port as reachable from SSH server.')


def _stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextmanager
def database_connection(database_url, args):
    if not args.ssh_host:
        yield database_url
        return
    from psycopg2.extensions import parse_dsn, make_dsn

    executable = shutil.which('ssh')
    if not executable:
        raise ValueError('OpenSSH client not found; install it or disable --ssh-host.')
    if args.ssh_host.startswith('-') or any(char.isspace() for char in args.ssh_host):
        raise ValueError('Invalid SSH host/alias.')
    if not 0 <= args.ssh_port <= 65535 or not 1 <= args.ssh_db_port <= 65535:
        raise ValueError('Invalid SSH or remote DB port.')
    if not re.fullmatch(r'[A-Za-z0-9_.:\-]+', args.ssh_db_host):
        raise ValueError('Invalid SSH remote DB hostname.')
    parameters = parse_dsn(database_url)
    if ',' in parameters.get('host', '') or ',' in parameters.get('hostaddr', ''):
        raise ValueError('SSH backup requires a single PostgreSQL endpoint.')
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    remote_host = args.ssh_db_host
    if ':' in remote_host:
        remote_host = f'[{remote_host}]'
    command = [executable, '-N', '-T', '-a', '-x',
               '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ExitOnForwardFailure=yes', '-o', 'ConnectTimeout=10',
               '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
               '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
               '-o', 'ForkAfterAuthentication=no']
    if args.ssh_key:
        key = Path(args.ssh_key).expanduser()
        if not key.is_file():
            raise ValueError('SSH key file not found; check --ssh-key (key contents are not read).')
        command += ['-o', 'IdentitiesOnly=yes', '-i', str(key)]
    if args.ssh_user:
        command += ['-l', args.ssh_user]
    if args.ssh_port:
        command += ['-p', str(args.ssh_port)]
    command += ['-L', f'127.0.0.1:{port}:{remote_host}:{args.ssh_db_port}', args.ssh_host]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise ValueError('SSH tunnel failed. Check SSH host/user/key, known_hosts and SSH access. No private diagnostics printed.')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError('SSH tunnel startup timed out; check SSH access.')
                time.sleep(0.1)
        parameters['hostaddr'] = '127.0.0.1'
        parameters.setdefault('host', 'localhost')
        parameters['port'] = str(port)
        # Keep original host and TLS settings; hostaddr changes only the network route.
        print('SSH tunnel ready (local loopback only).')
        yield make_dsn(**parameters)
    finally:
        _stop(process)
