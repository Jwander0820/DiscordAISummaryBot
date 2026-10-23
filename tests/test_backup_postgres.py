import importlib.util
import hashlib
import subprocess
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location('backup_postgres', Path(__file__).resolve().parents[1] / 'tools/backup_postgres.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class RemoteBackupTests(unittest.TestCase):
    def args(self, output):
        with patch.object(tool, 'load_local_env'), patch.dict('os.environ', {}, clear=True):
            return tool.parse_args(['--ssh-host', 'fixture-alias', '--output', str(output)])

    def fake_ssh(self, payload=b'PGDMP-fixture-archive', status=0, diagnostics=None):
        def start(command, **kwargs):
            process = MagicMock()
            process.returncode = status
            process.poll.return_value = status

            def communicate(input=None, timeout=None):
                kwargs['stdout'].write(payload)
                result = diagnostics
                if result is None:
                    result = b'PG_BACKUP_SHA256=' + hashlib.sha256(payload).hexdigest().encode() + b'\n'
                return None, result

            process.communicate.side_effect = communicate
            return process
        return start

    def test_success_streams_binary_and_publishes_only_archive(self):
        payload = b'PGDMP\x00\xff\x80\r\nfixture'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'backup.dump'
            with patch.object(tool.shutil, 'which', return_value='ssh'), patch.object(
                tool.subprocess, 'Popen', side_effect=self.fake_ssh(payload)
            ) as spawn:
                saved, digest = tool.download_backup(self.args(output))
            self.assertEqual(saved, output)
            self.assertEqual(saved.read_bytes(), payload)
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            self.assertEqual(list(Path(directory).iterdir()), [output])
            command = spawn.call_args.args[0]
            self.assertEqual(command[-2:], ['fixture-alias', 'sh -s'])
            self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertIn('ClearAllForwardings=yes', command)

    def test_failures_remove_partial_and_never_publish(self):
        cases = [
            (b'PGDMP-fixture', 1, b'private-password-and-host'),
            (b'PGDMP-fixture', 0, b'PG_BACKUP_SHA256=' + b'0' * 64 + b'\n'),
            (b'bad-header', 0, None),
            (b'PGDMP-fixture', 0, b'private-warning'),
        ]
        for payload, status, diagnostics in cases:
            with self.subTest(status=status, diagnostics=diagnostics), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'backup.dump'
                with patch.object(tool.shutil, 'which', return_value='ssh'), patch.object(
                    tool.subprocess, 'Popen', side_effect=self.fake_ssh(payload, status, diagnostics)
                ):
                    with self.assertRaises(ValueError) as caught:
                        tool.download_backup(self.args(output))
                    self.assertNotIn('private-password-and-host', str(caught.exception))
                    self.assertNotIn('private-warning', str(caught.exception))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_existing_file_is_preserved_without_starting_ssh(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'backup.dump'
            output.write_bytes(b'old-backup')
            with patch.object(tool.shutil, 'which', return_value='ssh'), patch.object(tool.subprocess, 'Popen') as spawn:
                with self.assertRaises(ValueError):
                    tool.download_backup(self.args(output))
                spawn.assert_not_called()
            self.assertEqual(output.read_bytes(), b'old-backup')

    def test_timeout_kills_ssh_and_cleans_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'backup.dump'
            process = MagicMock()
            process.poll.return_value = None
            process.communicate.side_effect = [subprocess.TimeoutExpired('ssh', 1), (None, b'')]
            with patch.object(tool.shutil, 'which', return_value='ssh'), patch.object(
                tool.subprocess, 'Popen', return_value=process
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    tool.download_backup(self.args(output))
            process.kill.assert_called_once()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_key_is_only_passed_as_a_path_and_remote_names_are_quoted(self):
        args = self.args('unused.dump')
        args.ssh_key = 'fixture.pem'
        args.database = "db'; printf injected; '"
        with patch.object(tool.shutil, 'which', return_value='ssh'), patch.object(
            Path, 'is_file', return_value=True
        ), patch.object(Path, 'read_bytes', side_effect=AssertionError('key read')), patch.object(
            Path, 'read_text', side_effect=AssertionError('key read')
        ):
            command = tool.build_ssh_command(args)
            script = tool.build_remote_script(args).decode()
        self.assertIn('fixture.pem', command)
        self.assertNotIn(args.database, command)
        self.assertIn(tool.shlex.quote(args.database), script)
        self.assertIn('trap', script)
        self.assertIn('pg_restore --file=/dev/null', script)
        self.assertIn('umask 077', script)

    def test_container_selection_error_is_actionable_without_raw_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'backup.dump'
            with patch.object(tool.shutil, 'which', return_value='ssh'), patch.object(
                tool.subprocess, 'Popen', side_effect=self.fake_ssh(status=1, diagnostics=b'PG_BACKUP_ERROR=container_selection\n')
            ):
                with self.assertRaisesRegex(ValueError, '--container'):
                    tool.download_backup(self.args(output))

    def test_docker_permission_error_suggests_sudo_without_raw_details(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'backup.dump'
            with patch.object(tool.shutil, 'which', return_value='ssh'), patch.object(
                tool.subprocess, 'Popen', side_effect=self.fake_ssh(status=1, diagnostics=b'permission denied on docker.sock private-host')
            ):
                with self.assertRaisesRegex(ValueError, '--sudo') as caught:
                    tool.download_backup(self.args(output))
                self.assertNotIn('private-host', str(caught.exception))


class FullBackupTests(unittest.TestCase):
    def test_complete_export_and_validation_keep_secret_out_of_arguments(self):
        commands = []

        def run(command, env):
            commands.append(command)
            self.assertEqual(env['PGDATABASE'], 'private-test-connection')
            self.assertNotIn('private-test-connection', ' '.join(command))
            if command[0] == 'pg_dump':
                Path(command[-1]).write_bytes(b'archive-test')

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'full.dump'
            with patch.object(tool, 'run_client', side_effect=run):
                digest = tool.backup_database(output, 'private-test-connection', 'pg_dump', 'pg_restore')
            self.assertEqual(output.read_bytes(), b'archive-test')
            self.assertEqual(len(digest), 64)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
        self.assertEqual(len(commands), 3)
        self.assertIn('--format=custom', commands[0])
        self.assertFalse(any(arg.startswith(('--table', '--schema', '--exclude')) for arg in commands[0]))
        self.assertEqual(commands[1][1], '--list')
        self.assertEqual(commands[2][1:3], ['--file', tool.os.devnull])


class UnifiedBackupTests(unittest.TestCase):
    def args(self, directory, *extra):
        with patch.object(tool, 'load_local_env'), patch.dict(os.environ, {}, clear=True):
            return tool.parse_args(['--output-dir', str(directory), *extra])

    def fake_format(self, failure=None):
        def run(kind, output, args):
            if kind == failure or failure == 'both':
                raise RuntimeError('fixture-password-and-private-host')
            output.write_bytes(kind.encode())
            result = {'status': 'success', 'file': output.name, 'bytes': len(kind), 'sha256': 'fixture-hash'}
            if kind == 'sqlite':
                result['tables'] = {'new_table': 3}
            return result
        return run

    def test_both_default_shares_one_directory_and_records_results(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory) / 'batch'
            with patch.object(tool, '_run_format', side_effect=self.fake_format()) as run:
                self.assertEqual(tool.run_backup(self.args(batch)), 0)
            self.assertEqual([call.args[0] for call in run.call_args_list], ['postgres', 'sqlite'])
            manifest = json.loads((batch / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['status'], 'success')
            self.assertEqual(manifest['snapshot_consistency'], 'independent')
            self.assertEqual(manifest['backups']['sqlite']['tables'], {'new_table': 3})
            self.assertEqual({file.name for file in batch.iterdir()}, {'database.dump', 'database.sqlite3', 'manifest.json'})

    def test_partial_and_total_failure_do_not_discard_success_or_leak_error(self):
        for failure in ('postgres', 'sqlite', 'both'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                batch = Path(directory) / 'batch'
                with patch.object(tool, '_run_format', side_effect=self.fake_format(failure)) as run:
                    self.assertEqual(tool.run_backup(self.args(batch)), 1)
                self.assertEqual(run.call_count, 2)
                raw = (batch / 'manifest.json').read_text(encoding='utf-8')
                self.assertNotIn('fixture-password', raw)
                self.assertNotIn('private-host', raw)
                manifest = json.loads(raw)
                self.assertEqual(manifest['status'], 'failed' if failure == 'both' else 'partial_success')
                for kind, filename in [('postgres', 'database.dump'), ('sqlite', 'database.sqlite3')]:
                    self.assertEqual((batch / filename).exists(), failure not in (kind, 'both'))

    def test_existing_batch_directory_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            batch = Path(directory)
            with patch.object(tool, '_run_format') as run:
                with self.assertRaises(FileExistsError):
                    tool.run_backup(self.args(batch))
                run.assert_not_called()

    def test_explicit_single_format_runs_only_requested_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(tool, '_run_format', side_effect=self.fake_format()) as run:
                self.assertEqual(tool.run_backup(self.args(Path(directory) / 'batch', '--format', 'sqlite')), 0)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], 'sqlite')

    def test_legacy_output_still_defaults_to_postgres_single_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'old-interface.dump'
            with patch.object(tool, 'load_local_env'), patch.dict(os.environ, {}, clear=True):
                args = tool.parse_args(['--output', str(output)])
            self.assertEqual(args.format, 'postgres')
            with patch.object(tool, '_run_format', side_effect=self.fake_format()) as run:
                self.assertEqual(tool.run_backup(args), 0)
            self.assertEqual(run.call_args.args[0], 'postgres')
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_remote_both_uses_database_url_db_name_not_container_default(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(Path(directory) / 'batch', '--ssh-host', 'fixture-alias', '--sudo')
            output = Path(directory) / 'native.dump'

            def download(remote):
                self.assertEqual(remote.database, 'expected_fixture_db')
                self.assertTrue(remote.sudo)
                remote.output.write_bytes(b'PGDMP-fixture')

            with patch.dict(os.environ, {'DATABASE_URL': 'dbname=expected_fixture_db user=fixture'}), patch.object(
                tool, 'download_backup', side_effect=download
            ):
                result = tool._run_format('postgres', output, args)
            self.assertEqual(result['status'], 'success')
            self.assertFalse(result['restore_tested'])
            self.assertEqual(len(result['sha256']), 64)

    def test_database_override_applies_to_sqlite_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args(Path(directory) / 'batch', '--database', 'explicit_fixture')
            with patch.dict(os.environ, {'DATABASE_URL': 'dbname=old_fixture user=fixture'}):
                from psycopg2.extensions import parse_dsn
                self.assertEqual(parse_dsn(tool._database_url(args))['dbname'], 'explicit_fixture')

class FullBackupFailureTests(unittest.TestCase):
    def test_failure_cleans_temporary_and_never_publishes(self):
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'full.dump'
                calls = []

                def run(command, env):
                    calls.append(command)
                    if len(calls) == fail_at:
                        raise RuntimeError('simulated failure')
                    if command[0] == 'pg_dump':
                        Path(command[-1]).write_bytes(b'archive-test')

                with patch.object(tool, 'run_client', side_effect=run):
                    with self.assertRaises(RuntimeError):
                        tool.backup_database(output, 'private', 'pg_dump', 'pg_restore')
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_existing_backup_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'full.dump'
            output.write_bytes(b'original')
            with patch.object(tool, 'run_client') as run:
                with self.assertRaises(RuntimeError):
                    tool.backup_database(output, 'private', 'pg_dump', 'pg_restore')
                run.assert_not_called()
            self.assertEqual(output.read_bytes(), b'original')

    def test_error_does_not_reveal_child_diagnostics(self):
        result = tool.subprocess.CompletedProcess([], 1, stderr=b'private host and password')
        with patch.object(tool.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError) as raised:
                tool.run_client(['pg_dump'], {})
        self.assertNotIn('private host', str(raised.exception))

    def test_publication_race_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'full.dump'

            def run(command, env):
                if command[0] == 'pg_dump':
                    Path(command[-1]).write_bytes(b'archive-test')
                    output.write_bytes(b'concurrent-backup')

            with patch.object(tool, 'run_client', side_effect=run):
                with self.assertRaises(FileExistsError):
                    tool.backup_database(output, 'private', 'pg_dump', 'pg_restore')
            self.assertEqual(output.read_bytes(), b'concurrent-backup')
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
